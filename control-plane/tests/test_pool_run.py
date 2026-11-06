"""`pool run` and `pool attach` (docs/design/pool-run.md, step 1). Building and pushing with a real Docker and
registry is in tests/e2e/test_pool_run.py; here: the command line, the plan, the local rules, the hints, and
attaching to a job a (fake) computer runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fake_agent import share

from pool_cli import hints
from pool_cli import main as cli
from pool_cli import run as pool_run
from pool_cp import storage
from pool_protocol.jobspec import load_job_spec

pytestmark = pytest.mark.asyncio

IMG = "registry.example.org/lab/app:1@sha256:" + "b" * 64
ROOT = Path(__file__).resolve().parents[2]


def parse(*argv: str) -> pool_run.RunOptions:
    return pool_run.parse(list(argv))


# --------------------------------------------------------------------------- the command line


async def test_docker_run_command_lines():
    o = parse("-e", "MODE=dev", "--gpus", "1", "--cpus", "2", "-m", "4g", "-w", "/app", "--rm", IMG, "--port",
              "8080", "-v")
    assert (o.image, o.args, o.env, o.gpu, o.cpu, o.memory, o.workdir) == (
        IMG, ["--port", "8080", "-v"], {"MODE": "dev"}, 1, 2.0, "4Gi", "/app")
    o = parse("--memory=512m", "--disk", "10g", "--", "python", "train.py", "--epochs", "3")
    assert o.image is None and o.args == ["python", "train.py", "--epochs", "3"], "after --: the arguments"
    assert (o.memory, o.disk) == ("512Mi", "10Gi")
    assert parse("--entrypoint", "sh", IMG, "-c", "echo hi").entrypoint == "sh"
    assert parse("-it", IMG).notes, "-it is accepted with a note: no interactive terminal yet"
    assert parse("-m", "2Gi", IMG).memory == "2Gi", "pool's own sizes pass through"


@pytest.mark.parametrize("argv, words", [
    (["-v", "./data:/data", IMG], "put the files into the image"),
    (["--volume=./d:/d", IMG], "put the files into the image"),
    (["--network", "host", IMG], "no network"),
    (["--privileged", IMG], "owner's computer"),
    (["-u", "0", IMG], "unprivileged"),
    (["-p", "8000:8000", IMG], "do not work yet"),
    (["--gpus", "all", IMG], "how many"),
    (["-m", "lots", IMG], "not a size"),
    (["-m", "512", IMG], "give a unit"),
    (["--frobnicate", IMG], "unknown option"),
    (["--cpus"], "needs a value"),
])
async def test_refused_and_wrong_options_say_why(argv, words):
    with pytest.raises(cli.ApiError, match=words):
        parse(*argv)


async def test_env_from_my_environment_and_env_files_like_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("SEED", "7")
    monkeypatch.setenv("FROM_HERE", "yes")
    monkeypatch.delenv("UNSET_HERE", raising=False)
    (tmp_path / "app.env").write_text("# settings\nMODE=test\nNAME='pool'\nFROM_HERE\nUNSET_HERE\n\n")
    o = parse("-e", "SEED", "--env-file", str(tmp_path / "app.env"), IMG)
    p = pool_run.plan(o, tmp_path)
    assert p.job["env"] == {"MODE": "test", "NAME": "'pool'", "FROM_HERE": "yes", "SEED": "7"}, \
        "values are literal, a bare KEY comes from here, an unset one is left out"
    monkeypatch.delenv("SEED")
    o = parse("-e", "SEED", IMG)
    assert "SEED" not in o.env and "not set here" in o.notes[0]
    assert parse("-dit", IMG).detach and len(parse("-it", IMG).notes) == 2
    with pytest.raises(cli.ApiError, match="not supported"):
        parse("--entrypoint", "", IMG)


async def test_the_plan_from_flags_and_pool_yaml(tmp_path):
    with pytest.raises(cli.ApiError, match="Dockerfile"):
        pool_run.plan(parse("--", "x"), tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM busybox\n")
    (tmp_path / "pool.yaml").write_text("name: trainer\ncommand: python train.py\nargs: --epochs 1\ncpu: 2\n"
                                        "memory: 4Gi\nenv: {MODE: prod}\ngroup: ml-lab\n")
    p = pool_run.plan(parse("--gpus", "1", "--", "--epochs", "9"), tmp_path)
    assert p.build_context == tmp_path.resolve() and p.dockerfile == tmp_path.resolve() / "Dockerfile"
    assert p.job == {"name": "trainer", "image": "pending-build", "command": "python train.py",
                     "args": ["--epochs", "9"], "cpu": 2, "memory": "4Gi", "gpu": 1, "retries": 0,
                     "env": {"MODE": "prod"}, "group": "ml-lab"}, "flags win over pool.yaml"
    assert p.platform == "linux/amd64"
    p = pool_run.plan(parse(IMG, "a"), tmp_path)
    assert p.build_context is None and p.job["image"] == IMG and p.job["args"] == ["a"], "an image skips the build"
    p = pool_run.plan(parse(IMG), tmp_path)
    assert "command" not in p.job and p.job["cpu"] == 2, "another image: pool.yaml's command is not for it"
    (tmp_path / "other.Dockerfile").write_text("FROM busybox\n")
    sub = tmp_path / "ctx"
    sub.mkdir()
    p = pool_run.plan(parse("--build", "ctx", "-f", "other.Dockerfile", "--", "x"), tmp_path)
    assert p.dockerfile == (tmp_path / "other.Dockerfile").resolve(), "-f is relative to where you run it"
    (tmp_path / "pool.yaml").write_text("port: 8000\n")
    with pytest.raises(cli.ApiError, match="do not work yet"):
        pool_run.plan(parse(IMG), tmp_path)
    (tmp_path / "pool.yaml").write_text("colour: red\n")
    with pytest.raises(cli.ApiError, match="unknown field"):
        pool_run.plan(parse(IMG), tmp_path)
    (tmp_path / "pool.yaml").unlink()
    with pytest.raises(cli.ApiError, match="workdir"):
        pool_run.plan(parse("-w", "app", IMG), tmp_path)


# --------------------------------------------------------------------------- --local: the same rules


async def test_local_runs_get_every_rule_of_a_members_computer():
    shared = json.loads((ROOT / "protocol" / "fixtures" / "sandbox_hardening.json").read_text())["flags"]
    job = {"image": IMG, "command": "python train.py", "args": ["--epochs", "2"], "memory": "512Mi", "cpu": 2,
           "env": {"MODE": "x"}, "workdir": "/app"}
    args = pool_run.local_args(job, "img:1", "/tmp/out", "pool-local-1")

    def has(seq):
        return any(args[i:i + len(seq)] == seq for i in range(len(args)))

    for flags in shared:
        assert has(flags), f"--local lacks {flags} from the shared rules"
    for seq in (["--memory", "512m"], ["--memory-swap", "512m"], ["--cpus", "2"], ["--workdir", "/app"],
                ["--mount", "type=bind,src=/tmp/out,dst=/out"], ["--env", "POOL_TASK_INDEX=0"],
                ["--env", "MODE=x"], ["--entrypoint", "python", "img:1", "train.py", "--epochs", "2"]):
        assert has(seq), seq
    plain = pool_run.local_args({"args": ["--port", "1"]}, "img:1", "/o", "n")
    assert plain[-3:] == ["img:1", "--port", "1"] and "--entrypoint" not in plain, "docker run IMAGE ARGS"
    filled = pool_run.local_args({"command": "sh -c 'echo {{index}} of {{count}}'"}, "img:1", "/o", "n")
    assert filled[-2:] == ["-c", "echo 0 of 1"], "placeholders are filled in as the server does"


async def test_my_sign_in_must_last_through_the_push():
    import base64 as b64
    import time as t

    def token(exp):
        body = b64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"x.{body}.y"

    pool_run._check_sign_in(token(t.time() + 3600), 600)
    with pytest.raises(cli.ApiError, match="pool login"):
        pool_run._check_sign_in(token(t.time() + 300), 600)
    pool_run._check_sign_in("not-a-jwt", 600)


@pytest.mark.parametrize("line", [
    "OSError: [Errno 30] Read-only file system: '/app/cache'",  # Python
    "open /app/cache: read-only file system",  # Go
    "java.nio.file.FileSystemException: /app/cache: Read-only file system",  # Java
    "Error: EROFS: read-only file system, mkdir '/app/cache'",  # Node
    "touch: /app/cache: Read-only file system",  # C (busybox)
])
async def test_hints_work_for_every_language(line):
    assert hints.match(line) == [hints._RULES[0][2]]


async def test_hints_for_network_permissions_and_memory():
    assert "no network" in hints.match("dial tcp: lookup pypi.org: no such host")[0]
    assert "no network" in hints.match("socket.gaierror: [Errno -3] Temporary failure in name resolution")[0]
    assert "unprivileged" in hints.match("PermissionError: [Errno 13] Permission denied: '/root/x'")[0]
    assert "memory limit (4Gi)" in hints.for_exit(137, "4Gi")[0]
    assert hints.match("all good") == [] and hints.for_exit(1, None) == []


# --------------------------------------------------------------------------- attaching to a real job


@pytest.fixture
def store(monkeypatch):
    """The file store as a dict, with downloads served from it (presigned URLs and httpx.stream)."""
    import httpx

    objects: dict[str, bytes] = {}

    class Stream:
        def __init__(self, url):
            self.status_code, self.data = 200, objects[url.rsplit("/", 1)[-1]]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield self.data

    monkeypatch.setattr(storage, "enabled", lambda settings: True)
    monkeypatch.setattr(storage, "object_exists", lambda settings, gid, sha: sha in objects)
    monkeypatch.setattr(storage, "presign_get", lambda settings, gid, sha, filename=None: f"http://store/{sha}")
    monkeypatch.setattr(httpx, "stream", lambda method, url, timeout: Stream(url))
    return objects


async def work(owner, g: str, agent):
    """The offer for the job `pool run` submits from its thread (the fake computer asks once it exists)."""
    for _ in range(100):
        if await owner.get(f"/api/v1/groups/{g}/jobs"):
            return await agent.ready()
        await asyncio.sleep(0.1)
    raise AssertionError("pool run did not submit a job")


def put(store: dict, path: str, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    store[sha] = data
    return {"path": path, "sha256": sha, "size": len(data)}


async def test_pool_run_submits_follows_and_saves_the_files(group_with_device, cli, store, tmp_path, monkeypatch,
                                                            capsys):
    owner, g, agent = await group_with_device()
    cli("login", "--server", cli.server, "--dev-user", owner.username, "--group", g)
    monkeypatch.chdir(tmp_path)
    await agent.connect([share(g)])
    run = asyncio.create_task(asyncio.to_thread(pool_run.run, ["-e", "MODE=x", IMG, "--epochs", "2"]))
    offer = await work(owner, g, agent)
    env = agent.open_envelope(offer)
    assert env.spec.args == ["--epochs", "2"] and env.spec.command == [] and env.spec.env == {"MODE": "x"}
    await agent.run_task(offer, logs="epoch 1\nepoch 2\n", outputs=[put(store, "model/weights.bin", b"\x00\x01")])
    assert await asyncio.wait_for(run, 30) == 0
    out = capsys.readouterr()
    assert "epoch 1\nepoch 2\n" in out.out and "Running on" in out.err and "1 file(s) saved" in out.err
    assert (tmp_path / "pool-out" / "model" / "weights.bin").read_bytes() == b"\x00\x01"


async def test_a_failing_program_returns_its_exit_code_with_a_hint(group_with_device, cli, tmp_path, monkeypatch,
                                                                   capsys):
    owner, g, agent = await group_with_device()
    cli("login", "--server", cli.server, "--dev-user", owner.username, "--group", g)
    monkeypatch.chdir(tmp_path)
    await agent.connect([share(g)])
    run = asyncio.create_task(asyncio.to_thread(pool_run.run, [IMG]))
    offer = await work(owner, g, agent)
    await agent.run_task(offer, exit_code=3, outputs=[],
                         logs="OSError: [Errno 30] Read-only file system: '/app/cache'\n")
    assert await asyncio.wait_for(run, 30) == 3
    assert "read-only. Write to /out" in capsys.readouterr().err


async def test_detach_then_attach_and_stop(group_with_device, cli, tmp_path, monkeypatch, capsys):
    owner, g, agent = await group_with_device()
    cli("login", "--server", cli.server, "--dev-user", owner.username, "--group", g)
    monkeypatch.chdir(tmp_path)
    assert pool_run.run(["-d", IMG]) == 0
    err = capsys.readouterr().err
    job_id = err.split("pool attach ")[1].split()[0]
    assert job_id in err
    # Ctrl-C while attached, not on a terminal: the job is stopped without asking.
    assert pool_run._stop(job_id, yes=False) == 130
    assert (await owner.get(f"/api/v1/jobs/{job_id}"))["state"] == "cancelled"
    assert pool_run.attach(job_id, tmp_path / "out") == 130, "attaching to a finished job reports how it ended"


@pytest.mark.parametrize("folder", sorted(p for p in (ROOT / "examples" / "run").iterdir() if p.is_dir()),
                         ids=lambda p: p.name)
async def test_every_pool_run_example_is_a_valid_project(folder):
    p = pool_run.plan(parse(), folder)
    assert p.build_context == folder.resolve() and p.dockerfile.exists()
    # Plain `pool run` must be enough: the job the folder makes is one the server accepts.
    spec = load_job_spec({**p.job, "image": "registry.example/e2e/me/x@sha256:" + "0" * 64})
    assert spec.resources().memory_mb <= 1024, "examples fit the default memory, so they need no settings"
