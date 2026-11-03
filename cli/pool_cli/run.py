"""`pool run` and `pool attach` (docs/design/pool-run.md, step 1).

`pool run` takes `docker run`'s flags: what runs with `docker run IMAGE ARGS` on your computer runs with
`pool run IMAGE ARGS` on a computer of your group. Without an image it builds the Dockerfile in the current
folder on your computer and pushes it to the group's built-in registry. It stays attached: output streams to
your terminal, Ctrl-C stops the job, and its /out files are saved in ./pool-out when it ends.

`pool run --local` runs the same job with your own Docker and the rules of a member's computer
(pool_protocol.sandbox), so a program that would fail there fails here first.

`pool attach JOB` does the attaching for any job, also one submitted in the browser.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pool_protocol import sandbox
from pool_protocol.jobspec import JobSpecError, default_name, load_job_spec, parse_quantity_mb, result_folder

from . import hints
from . import main as cli

PROJECT_FILE = "pool.yaml"
_PROJECT_KEYS = {"name", "group", "image", "build", "platform", "command", "args", "workdir", "env", "cpu",
                 "memory", "disk", "gpu", "time_limit", "retries"}

# docker run flags pool will not take, and why (they would open the owner's computer, or need what a borrowed
# computer does not have).
REFUSED = {
    "-v": "your folders are not on that computer: put the files into the image (COPY in the Dockerfile)",
    "--volume": "your folders are not on that computer: put the files into the image (COPY in the Dockerfile)",
    "--mount": "your folders are not on that computer: put the files into the image (COPY in the Dockerfile)",
    "--network": "jobs have no network, to protect the owner's network",
    "--net": "jobs have no network, to protect the owner's network",
    "--add-host": "jobs have no network, to protect the owner's network",
    "--dns": "jobs have no network, to protect the owner's network",
    "--privileged": "it would give the job the owner's computer",
    "--cap-add": "jobs run without Linux capabilities",
    "--device": "devices of the owner's computer are not shared (GPUs: --gpus)",
    "--pid": "jobs cannot see the owner's processes",
    "--ipc": "jobs cannot share memory with the owner's processes",
    "--uts": "jobs cannot change the owner's host name",
    "--userns": "jobs run as an unprivileged user",
    "-u": "jobs run as an unprivileged user (uid 65534)",
    "--user": "jobs run as an unprivileged user (uid 65534)",
    "--security-opt": "the owner's security settings apply",
    "--sysctl": "the owner's kernel settings apply",
    "--runtime": "the owner's computer chooses the runtime",
    "--cgroup-parent": "the owner's computer sets the limits",
}
LATER = {"-p", "--publish", "-P", "--publish-all", "--expose"}
NOTED = {"-i", "-t", "-it", "-ti", "--interactive", "--tty"}
IGNORED = {"--rm", "--init"}
_VALUE_OPTS = {"-e", "--env", "--env-file", "--gpus", "--gpu", "--cpus", "--cpu", "-m", "--memory", "--disk", "-w",
               "--workdir", "--entrypoint", "--name", "--group", "--time-limit", "--retries", "--build", "-f",
               "--file", "--platform", "-o", "--out"}
_FLAG_OPTS = {"-d", "--detach", "--local", "-y", "--yes", "-h", "--help"}

USAGE = """\
Usage: pool run [OPTIONS] [IMAGE] [ARG...]
       pool run [OPTIONS] -- [ARG...]        (build ./Dockerfile, or the image in ./pool.yaml)

Run a program on a computer of your group, like `docker run` on yours.

  IMAGE                  An image (ghcr.io/you/app:1, python:3.12-slim). Without one, the Dockerfile here is
                         built on your computer and pushed to the group's registry.
  ARG...                 Arguments for the image's command, as with `docker run IMAGE ARG...`.

  -e, --env KEY=VALUE    Environment variable (not for real secrets: the computer's owner can read it)
      --env-file FILE    Environment variables from a file
      --cpus N           CPU cores (default 1)             -m, --memory SIZE   Memory, e.g. 512m, 4g (default 1g)
      --gpus N           GPUs                                  --disk SIZE     Disk, e.g. 10g (default 1g)
  -w, --workdir PATH     Working directory in the container    --entrypoint CMD  Run CMD instead of the
      --time-limit T     Stop after T, e.g. 30m, 2h (1h)                           image's entrypoint
      --retries N        Tries again after a failure (0)
      --build PATH       Build this folder (default: .)    -f, --file FILE     Dockerfile to build
      --platform P       Platform to build for (default linux/amd64)
      --name NAME        Job name (default: from the folder or image)
      --group GROUP      Group (default: from `pool login --group`)
  -o, --out DIR          Where the job's /out files go (default ./pool-out)
  -d, --detach           Start it and return; `pool attach JOB` later
      --local            Run it here, with your Docker and the rules of a member's computer
  -y, --yes              Ctrl-C stops the job without asking

Settings can also live in ./pool.yaml (a job file plus `build` and `platform`); flags win.
"""


@dataclass
class RunOptions:
    image: str | None = None
    args: list[str] | None = None  # None: not given (pool.yaml's args apply)
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(default_factory=list)
    gpu: int | None = None
    cpu: float | None = None
    memory: str | None = None
    disk: str | None = None
    workdir: str | None = None
    entrypoint: str | None = None
    name: str | None = None
    group: str | None = None
    time_limit: str | None = None
    retries: int | None = None
    build: str | None = None
    dockerfile: str | None = None
    platform: str | None = None
    out: str = "pool-out"
    detach: bool = False
    local: bool = False
    yes: bool = False
    notes: list[str] = field(default_factory=list)


def docker_size(text: str, what: str) -> str:
    """docker's sizes (512m, 4g, 1024k) as pool's (512Mi, 4Gi, 1024Ki); pool's own pass through."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bkmgBKMG])\s*", text)
    if m:
        number, unit = m.group(1), m.group(2).lower()
        if unit == "b":
            return f"{max(1, int(float(number)) // (1024 * 1024))}Mi"
        return f"{number}{unit.upper()}i"
    try:
        parse_quantity_mb(text)
    except JobSpecError:
        raise cli.ApiError(f"{what}: {text!r} is not a size; write e.g. 512m or 4g") from None
    if text.strip().isdigit():
        raise cli.ApiError(f"{what}: give a unit, e.g. {text.strip()}m or 4g")
    return text.strip()


def parse(argv: list[str]) -> RunOptions:
    """docker run's command line: options, then the image (or --), then the arguments for the image."""
    o = RunOptions()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            o.args = argv[i + 1:]
            return o
        if not a.startswith("-") or a == "-":
            o.image, o.args = a, argv[i + 1:]
            return o
        if re.fullmatch(r"-[dit]{2,3}", a):  # -it, -dit: docker's short flags written together
            argv = argv[:i] + [f"-{c}" for c in a[1:]] + argv[i + 1:]
            a = argv[i]
        name, eq, inline = a.partition("=") if a.startswith("--") else (a, "", "")
        if name in REFUSED:
            raise cli.ApiError(f"pool run does not take {name}: {REFUSED[name]}.")
        if name in LATER:
            raise cli.ApiError(f"{name} (ports) comes in the next step of pool run (docs/design/pool-run.md §5). "
                               "Programs that only compute and write files work now.")
        if name in IGNORED:
            i += 1
            continue
        if name in NOTED:
            o.notes.append("pool run has no interactive terminal yet: the output streams here, input is empty.")
            i += 1
            continue
        if name not in _VALUE_OPTS and name not in _FLAG_OPTS:
            raise cli.ApiError(f"unknown option {name} (pool run --help lists the options)")
        if name in _FLAG_OPTS:
            if eq:
                raise cli.ApiError(f"{name} takes no value")
            if name in ("-h", "--help"):
                raise _Help
            setattr(o, {"-d": "detach", "--detach": "detach", "--local": "local", "-y": "yes", "--yes": "yes"}[name],
                    True)
            i += 1
            continue
        if eq:
            value = inline
        else:
            i += 1
            if i >= len(argv):
                raise cli.ApiError(f"{name} needs a value")
            value = argv[i]
        _set(o, name, value)
        i += 1
    return o


class _Help(Exception):  # noqa: N818 - control flow, not an error
    pass


def _set(o: RunOptions, name: str, value: str) -> None:  # noqa: C901 - one branch per option
    if name in ("-e", "--env"):
        key, eq, val = value.partition("=")
        if not eq:  # docker -e KEY takes the value from your environment, and leaves it out when unset
            if key not in os.environ:
                o.notes.append(f"-e {key}: {key} is not set here, so the job does not get it")
                return
            val = os.environ[key]
        o.env[key] = val
    elif name == "--env-file":
        o.env_files.append(value)
    elif name in ("--gpus", "--gpu"):
        if not value.isdigit():
            raise cli.ApiError(f"--gpus: say how many, e.g. --gpus 1 (not {value!r})")
        o.gpu = int(value)
    elif name in ("--cpus", "--cpu"):
        try:
            o.cpu = float(value)
        except ValueError:
            raise cli.ApiError(f"--cpus: {value!r} is not a number") from None
    elif name in ("-m", "--memory"):
        o.memory = docker_size(value, "--memory")
    elif name == "--disk":
        o.disk = docker_size(value, "--disk")
    elif name in ("-w", "--workdir"):
        o.workdir = value
    elif name == "--entrypoint":
        if not value:
            raise cli.ApiError("--entrypoint '' (no entrypoint) is not supported: give the command to run")
        o.entrypoint = value
    elif name == "--name":
        o.name = value
    elif name == "--group":
        o.group = value
    elif name == "--time-limit":
        o.time_limit = value
    elif name == "--retries":
        if not value.isdigit():
            raise cli.ApiError(f"--retries: {value!r} is not a whole number")
        o.retries = int(value)
    elif name == "--build":
        o.build = value
    elif name in ("-f", "--file"):
        o.dockerfile = value
    elif name == "--platform":
        o.platform = value
    elif name in ("-o", "--out"):
        o.out = value


def read_env_file(path: str) -> dict[str, str]:
    """docker's --env-file: KEY=VALUE taken literally (no quotes removed), KEY alone from your environment,
    # for comments."""
    out: dict[str, str] = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        raise cli.ApiError(f"--env-file {path}: {exc.strerror}") from None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, eq, val = line.partition("=")
        key = key.strip()
        if not eq:
            if key in os.environ:
                out[key] = os.environ[key]
            continue
        out[key] = val
    return out


def load_project(folder: Path) -> dict[str, Any]:
    """./pool.yaml: a short job file plus `build` and `platform`. Missing: {}."""
    path = folder / PROJECT_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise cli.ApiError(f"{PROJECT_FILE} must be a mapping, e.g. name: ..., command: ...")
    if "port" in data or "ports" in data:
        raise cli.ApiError(f"{PROJECT_FILE}: ports come in the next step of pool run (docs/design/pool-run.md §5)")
    unknown = sorted(set(data) - _PROJECT_KEYS)
    if unknown:
        raise cli.ApiError(f"{PROJECT_FILE}: unknown field(s) {', '.join(unknown)}; allowed: "
                           f"{', '.join(sorted(_PROJECT_KEYS))}")
    return data


@dataclass
class Plan:
    """What to run, from the flags and ./pool.yaml."""

    job: dict[str, Any]  # the short job file to submit (image filled in after a build)
    build_context: Path | None
    dockerfile: Path | None
    platform: str
    name: str


def plan(o: RunOptions, folder: Path) -> Plan:
    project = load_project(folder)
    env = {**(project.get("env") or {})}
    for f in o.env_files:
        env.update(read_env_file(f))
    env.update(o.env)
    if o.image:  # an image given here is run as given: pool.yaml's build and command are for its own image
        project = {k: v for k, v in project.items() if k not in ("image", "build", "command", "args", "workdir")}
    image = o.image or (None if o.build else project.get("image"))
    build = o.build or (project.get("build") if not o.image else None)
    if isinstance(build, dict):  # like Compose: the dockerfile is relative to the build folder
        dockerfile = (folder / build.get("context", ".") / build["dockerfile"]) if build.get("dockerfile") else None
        build = build.get("context", ".")
    else:
        dockerfile = None
    if o.dockerfile:  # like docker build -f: relative to where you run it
        dockerfile = folder / o.dockerfile
    if image is None and build is None:
        if (dockerfile or folder / "Dockerfile").exists():
            build = "."
        else:
            raise cli.ApiError("give an image (pool run IMAGE ...), or run it in a folder with a Dockerfile")
    context = (folder / build).resolve() if build is not None else None
    df = (dockerfile or context / "Dockerfile").resolve() if context else None
    if df is not None and not df.exists():
        raise cli.ApiError(f"no Dockerfile at {df}")
    name = o.name or project.get("name") or (default_name(image) if image else _folder_name(context or folder))
    job: dict[str, Any] = {"name": name, "image": image or "pending-build"}
    if o.entrypoint:
        job["command"] = [o.entrypoint]
    elif project.get("command"):
        job["command"] = project["command"]
    args = o.args if o.args is not None else project.get("args")
    if args:
        job["args"] = args
    for key, flag in (("workdir", o.workdir), ("cpu", o.cpu), ("memory", o.memory), ("disk", o.disk),
                      ("gpu", o.gpu), ("time_limit", o.time_limit), ("retries", o.retries)):
        value = flag if flag is not None else project.get(key)
        if value is not None:
            job[key] = value
    job.setdefault("retries", 0)  # like docker run: a failing program is reported, not run again
    if env:
        job["env"] = env
    group = o.group or project.get("group")
    if group:
        job["group"] = group
    try:
        load_job_spec(job)  # the server checks again; this catches mistakes before a build
    except JobSpecError as exc:
        raise cli.ApiError(f"job: {exc}") from None
    return Plan(job=job, build_context=context, dockerfile=df, platform=o.platform or project.get("platform")
                or "linux/amd64", name=name)


def _folder_name(folder: Path) -> str:
    return default_name(folder.resolve().name)


# ------------------------------------------------------------------------------------------ docker


def _docker() -> str:
    """The full path of the user's own Docker command."""
    path = shutil.which("docker")
    if path is None:
        raise cli.ApiError("pool run builds and checks images with Docker on your computer: install Docker first")
    return path


def docker_build(p: Plan, tag: str) -> None:
    cmd = [_docker(), "build", "--platform", p.platform, "-f", str(p.dockerfile), "-t", tag, str(p.build_context)]
    _say(f"Building {p.dockerfile.relative_to(Path.cwd()) if p.dockerfile.is_relative_to(Path.cwd()) else p.dockerfile}"
         f" for {p.platform} ...")
    if subprocess.run(cmd).returncode != 0:
        raise cli.ApiError("the image did not build (docker build's output is above)")


def _context_env() -> dict[str, str]:
    """The environment for a docker command with its own --config: a throwaway config has no contexts, so
    point it at the Docker you use now (Docker Desktop, colima, rootless) unless DOCKER_HOST already does."""
    env = dict(os.environ)
    if not env.get("DOCKER_HOST"):
        r = subprocess.run([_docker(), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                           capture_output=True, text=True)
        host = r.stdout.strip()
        if r.returncode == 0 and host:
            env["DOCKER_HOST"] = host
    return env


def docker_push(tag: str, registry: str, handle: str, token: str) -> str:
    """Push with a throwaway Docker config holding only pool's sign-in; returns name@digest. Your own Docker
    logins are not touched, and the token never appears on a command line."""
    with tempfile.TemporaryDirectory(prefix="pool-push-") as cfg:
        auth = base64.b64encode(f"{handle}:{token}".encode()).decode()
        Path(cfg, "config.json").write_text(json.dumps({"auths": {registry: {"auth": auth}}}))
        os.chmod(Path(cfg, "config.json"), 0o600)
        r = subprocess.run([_docker(), "--config", cfg, "push", tag], capture_output=True, text=True,
                           env=_context_env())
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise cli.ApiError("pushing the image to the group's registry failed:\n" + out.strip()[-1500:])
    pushed = len(re.findall(r": Pushed\b", out))
    kept = len(re.findall(r"already exists", out, re.I))
    m = re.search(r"digest: (sha256:[0-9a-f]{64})", out)
    digest = m.group(1) if m else _repo_digest(tag)
    if digest is None:
        raise cli.ApiError("pushed, but Docker did not report the image's digest")
    _say(f"Uploaded {pushed} new layer(s), {kept} already there.")
    return f"{tag}@{digest}"


def _repo_digest(tag: str) -> str | None:
    repo = tag.rsplit(":", 1)[0]
    r = subprocess.run([_docker(), "image", "inspect", "--format", "{{json .RepoDigests}}", tag],
                       capture_output=True, text=True)
    try:
        digests = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    return next((d.split("@", 1)[1] for d in digests if d.startswith(repo + "@")), None)


def local_args(job: dict[str, Any], image: str, out_dir: str, name: str) -> list[str]:
    """`docker run` with the rules of a member's computer (pool_protocol.sandbox, as the agent applies them), for
    the task the server would make of this job: {{index}} and {{count}} are filled in the same way."""
    try:
        t = load_job_spec({**job, "image": image}).render_task({}, 0, 1)
    except JobSpecError as exc:
        raise cli.ApiError(f"job: {exc}") from None
    mb = t.resources.memory_mb
    args = [_docker(), "run", "--name", name]
    for flags in sandbox.HARDENING_FLAGS:
        args += flags
    args += ["--memory", f"{mb}m", "--memory-swap", f"{mb}m", "--cpus", f"{t.resources.cpu:g}",
             "--stop-timeout", "30", "--mount", f"type=bind,src={out_dir},dst={sandbox.OUTPUTS_PATH}"]
    if t.resources.gpus:
        args += ["--gpus", str(t.resources.gpus)]
    env = {**t.env, "POOL_TASK_INDEX": "0", "POOL_TASK_COUNT": "1", "POOL_OUTPUT_DIR": sandbox.OUTPUTS_PATH,
           "POOL_TASK_ID": "local", "POOL_ATTEMPT_ID": "local", "POOL_EPOCH": "1"}
    for k in sorted(env):
        args += ["--env", f"{k}={env[k]}"]
    if t.workdir:
        args += ["--workdir", t.workdir]
    if t.command:
        return args + ["--entrypoint", t.command[0], image, *t.command[1:], *t.args]
    return args + [image, *t.args]


def run_local(p: Plan, out: Path) -> int:
    image = p.job["image"]
    if p.build_context is not None:
        image = f"pool-local/{default_name(p.name)}:latest"
        docker_build(p, image)
    out_dir = tempfile.mkdtemp(prefix="pool-out-")
    os.chmod(out_dir, 0o777)  # the program runs as uid 65534  # noqa: S103
    name = f"pool-local-{uuid.uuid4().hex[:8]}"
    cmd = local_args(p.job, image, out_dir, name)
    _say(f"Running here with the rules of a member's computer: no network, read-only file system, user 65534, "
         f"{p.job.get('cpu', 1)} CPU, {p.job.get('memory', '1Gi')} memory.")
    limit = _seconds(p.job.get("time_limit", "1h"))
    timed_out = threading.Event()

    def stop_at_limit() -> None:
        timed_out.set()
        subprocess.run([_docker(), "stop", "-t", "10", name], capture_output=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    seen: list[str] = []
    timer = threading.Timer(limit, stop_at_limit)
    timer.start()
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            _remember(seen, line)
        code = proc.wait()
        _collect_local(name, out)
    except KeyboardInterrupt:
        _say("stopped")
        return 130
    finally:
        timer.cancel()
        subprocess.run([_docker(), "rm", "-f", name], capture_output=True)
        shutil.rmtree(out_dir, ignore_errors=True)  # files the job made unreadable stay behind; harmless
    if timed_out.is_set():
        _say(f"stopped: it ran longer than its time limit ({p.job.get('time_limit', '1h')}); raise it with "
             "--time-limit")
        return code or 124
    for h in hints.match("".join(seen)) + hints.for_exit(code, p.job.get("memory")):
        _say(h)
    _say(f"exit {code}" if code else "done")
    return code


def _collect_local(container: str, dest: Path) -> None:
    """The job's /out files, read through Docker (as the agent does), so files the program made unreadable for
    you still come back. Only regular files, inside dest."""
    proc = subprocess.Popen([_docker(), "cp", f"{container}:{sandbox.OUTPUTS_PATH}", "-"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    files = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for m in tar:
                parts = Path(m.name).parts[1:]  # the archive starts with the folder name, "out"
                if not parts or not m.isfile():
                    continue
                m.name = str(Path(*parts))
                dest.mkdir(parents=True, exist_ok=True)
                tar.extract(m, dest, filter="data")
                files += 1
    except tarfile.TarError as exc:
        _say(f"could not read the job's files: {exc}")
    proc.wait()
    if files:
        _say(f"{files} file(s) saved to {dest}/")


def _seconds(limit: str | int) -> float:
    from pool_protocol.jobspec import parse_duration_s

    return float(parse_duration_s(limit))


def _remember(seen: list[str], line: str) -> None:
    seen.append(line)
    if len(seen) > 2000:
        del seen[:1000]


def _say(msg: str) -> None:
    sys.stderr.write(f"pool: {msg}\n")
    sys.stderr.flush()


# ------------------------------------------------------------------------------------------ remote


def run(argv: list[str]) -> int:
    try:
        o = parse(argv)
    except _Help:
        sys.stdout.write(USAGE)
        return 0
    for note in o.notes:
        _say(note)
    p = plan(o, Path.cwd())
    out = Path(o.out)
    if o.local:
        return run_local(p, out)
    group = cli._group(p.job.pop("group", None))
    if p.build_context is not None:
        info = cli.api("GET", f"/api/v1/groups/{group}/registry")
        if not info.get("enabled"):
            raise cli.ApiError("this server has no built-in registry: give an image you pushed elsewhere "
                               "(pool run IMAGE ...)")
        token = cli.Config.load().token
        if not token:
            raise cli.ApiError("not logged in: run `pool login`")
        _check_sign_in(token, 600)
        stamp = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        tag = f"{info['repository_prefix']}/{default_name(p.name)}:run-{stamp}"
        me = cli.api("GET", "/api/v1/me")
        try:
            docker_build(p, tag)
            _check_sign_in(token, 120)
            p.job["image"] = docker_push(tag, info["address"], me["handle"], token)
        except KeyboardInterrupt:
            _say("stopped before the job was submitted")
            return 130
    job = cli.api("POST", f"/api/v1/groups/{group}/jobs", json=p.job)
    _say(f"Submitted {job['name']} ({job['tasks']['total']} task(s)) to {group}: job {job['id']}")
    if o.detach:
        _say(f"Running in the background. Follow it with: pool attach {job['id']}")
        return 0
    return attach(job["id"], out, yes=o.yes)


def _check_sign_in(token: str, seconds: int) -> None:
    """Docker signs in to the group's registry with your pool token: make sure it lasts through the push."""
    try:
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return  # not a JWT: the server decides
    if isinstance(exp, int | float) and exp - time.time() < seconds:
        left = max(0, int(exp - time.time()) // 60)
        raise cli.ApiError(f"your sign-in ends in {left} minute(s), before the image is uploaded: run `pool login` "
                           "again first")


def attach(job_id: str, out: Path, yes: bool = False, poll: float = 1.0) -> int:
    """Follow a job until it ends: output (one task) or progress (several), then save its /out files."""
    job = cli.api("GET", f"/api/v1/jobs/{job_id}")
    single = job["tasks"].get("total", 0) == 1
    cursor, attempt, seen, placed, progress, drained = 0, None, [], False, "", False
    try:
        while True:
            rows = []
            if single:
                rows = cli.api("GET", f"/api/v1/jobs/{job_id}/tasks/0/logs", params={"after_id": cursor,
                                                                                   "limit": 1000}) or []
                for r in rows:
                    cursor = max(cursor, r["id"])
                    if r["attempt_id"] != attempt:
                        if attempt is not None:
                            _say("this computer stopped the task; it starts again on another one")
                        attempt = r["attempt_id"]
                    _print_log(r)
                    _remember(seen, r["chunk"])
            job = cli.api("GET", f"/api/v1/jobs/{job_id}")
            if single and not placed and (rows or job["tasks"].get("running") or _finished(job)):
                tasks = cli.api("GET", f"/api/v1/jobs/{job_id}/tasks")
                if tasks and tasks[0].get("device"):
                    placed = True
                    _say(f"Ran on {tasks[0]['device']}" if _finished(job) and not rows
                         else f"Running on {tasks[0]['device']}")
            if not single:
                now = cli._fmt(job["tasks"])
                if now != progress:
                    progress = now
                    _say(now)
            if _finished(job):
                # Read the output once more after the job ended: its last lines may land just before.
                if not rows and drained:
                    break
                drained = True
                continue
            if not rows:
                time.sleep(poll)
    except KeyboardInterrupt:
        return _stop(job_id, yes)
    tasks = cli.api("GET", f"/api/v1/jobs/{job_id}/tasks")
    _save_outputs(job_id, tasks, out, single)
    codes = [(t.get("result") or {}).get("exit_code") for t in tasks]
    for h in hints.match("".join(seen)) + (hints.for_exit(codes[0], None) if single else []):
        _say(h)
    states = {t["state"] for t in tasks}
    if states == {"succeeded"}:
        _say("done")
        return 0
    if "cancelled" in states or job["state"] == "cancelled":
        _say("cancelled")
        return 130
    failed = [t for t in tasks if t["state"] == "failed"]
    _say(f"{job['state']}: {len(failed)} of {len(tasks)} task(s) failed"
         + ("" if single else f" (pool logs {job_id} --task N shows each one)"))
    code = codes[0] if single else None
    return code if isinstance(code, int) and 0 < code < 256 else 1


def _finished(job: dict) -> bool:
    """Every task has ended (the job's own state follows a moment later, from the scheduler)."""
    counts = job["tasks"]
    ended = sum(counts.get(k, 0) for k in cli.TERMINAL)
    return job["state"] in cli.TERMINAL or (counts.get("total", 0) > 0 and ended >= counts["total"])


def _print_log(r: dict) -> None:
    stream = r.get("stream")
    if stream in ("agent", "system"):
        for line in r["chunk"].rstrip("\n").splitlines():
            _say(line.removeprefix("agent: "))
        return
    target = sys.stderr if stream == "stderr" else sys.stdout
    target.write(r["chunk"])
    target.flush()


def _stop(job_id: str, yes: bool) -> int:
    if not yes and sys.stdin.isatty():
        try:
            answer = input("\nStop the job? [Y/n] ")
        except (KeyboardInterrupt, EOFError):
            answer = "n"
        if answer.strip().lower() in ("n", "no"):
            _say(f"left running. Follow it again with: pool attach {job_id}")
            return 0
    cli.api("POST", f"/api/v1/jobs/{job_id}/cancel")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _finished(cli.api("GET", f"/api/v1/jobs/{job_id}")):
            break
        time.sleep(0.5)
    _say("stopped")
    return 130


def _save_outputs(job_id: str, tasks: list[dict], out: Path, single: bool) -> None:
    files = 0
    for t in tasks:
        outputs = (t.get("result") or {}).get("outputs") or []
        folder = out if single else out / result_folder(t["index"], len(tasks), t.get("params") or {})
        for o in outputs:
            try:
                cli._fetch(job_id, t["index"], o, cli.safe_target(folder, o["path"]))
            except cli.ApiError as exc:
                if "no file store" in str(exc):
                    _say("the server has no file store, so the job's files stayed on the computer that ran it")
                    return
                raise
            files += 1
    if files:
        _say(f"{files} file(s) saved to {out}/")
