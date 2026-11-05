"""The `pool` CLI against a real server (plan §9 Phase 1 exit: a researcher runs a job from the CLI)."""

import textwrap

import pytest
from conftest import IMG, gname
from fake_agent import share
from typer.testing import CliRunner

from pool_cli.main import app

pytestmark = pytest.mark.asyncio


async def test_cli_flow(cli, tmp_path, enrolled, user, http):
    g = gname()
    assert "created group" in cli("groups", "create", g, "--template", "research-team").output
    spec = tmp_path / "job.yaml"
    spec.write_text(f"""
apiVersion: pool/v1alpha1
kind: Job
metadata: {{name: sweep, group: {g}}}
spec:
  shape: array
  image: {IMG}
  command: ["train", "--seed", "{{{{seed}}}}"]
  matrix: {{seed: [0, 1]}}
  resources: {{cpu: 1, memory: 256Mi}}
  max_runtime: 5m
""")
    out = cli("sweep", str(spec), "--set", "seed=0..9", "--set", "lr=0.1,0.01", "--dry-run").output
    assert out.startswith("20 tasks")
    out = cli("submit", str(spec)).output
    job_id = out.strip().split("id=")[1].split()[0]
    assert "2 task(s)" in out
    assert "sweep" in cli("jobs", "list", g).output
    status = cli("status", job_id).output
    assert "pending" in status and "seed=1" in status
    bad = tmp_path / "bad.yaml"
    bad.write_text(spec.read_text().replace(IMG, "python 3.12"))
    res = cli("submit", str(bad), ok=False)
    assert res.exit_code != 0
    inv = cli("invite", "create", g, "--role", "submitter").output
    assert "invite token" in inv
    assert "coordinator" in cli("members", "list", g).output
    # A device enrolled by the CLI user runs a task, then logs are readable from the CLI.
    from pool_cli.config import Config

    cfg = Config.load()
    from conftest import Client

    me = Client(http, "cli", cfg.token)
    agent = await enrolled(me)
    cli("devices", "share", agent.device_id, g)
    await agent.connect([share(g)])
    offer = await agent.ready()
    await agent.run_task(offer, logs="epoch 1 loss 0.5\n")
    index = agent.open_envelope(offer).spec.command[2]  # matrix seed == task index here
    assert "epoch 1 loss 0.5" in cli("logs", job_id, "--task", index).output
    hist = cli("devices", "history", agent.device_id).output
    assert "sweep" in hist and "succeeded" in hist
    await agent.close()


async def test_download_refuses_paths_outside_the_folder(tmp_path):
    from pool_cli.main import ApiError, safe_target

    assert safe_target(tmp_path, "result.txt") == (tmp_path / "result.txt").resolve()
    assert safe_target(tmp_path, "sub/dir/x.bin") == (tmp_path / "sub/dir/x.bin").resolve()
    for bad in ("../escape.txt", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(ApiError):
            safe_target(tmp_path, bad)


async def test_cli_download_explains_missing_file_store(cli, tmp_path, enrolled, user):
    g = gname()
    cli("groups", "create", g, "--template", "research-team")
    owner_name = cli("whoami").output.split()[0]
    owner = await user(owner_name)
    agent = await enrolled(owner)
    await agent.connect([share(g)])
    spec = tmp_path / "one.yaml"
    spec.write_text(f"""
apiVersion: pool/v1alpha1
kind: Job
metadata: {{name: one, group: {g}}}
spec:
  image: {IMG}
  command: ["sh", "-c", "echo hi > /out/result.txt"]
""")
    job_id = cli("submit", str(spec)).output.strip().split("id=")[1].split()[0]
    assert (await agent.run_task(await agent.ready())).accepted
    res = cli("download", job_id, "-o", str(tmp_path / "out"), ok=False)
    assert res.exit_code != 0 and "no file store" in str(res.exception), res.exception
    assert not (tmp_path / "out").exists(), "nothing is written when the download cannot start"


async def test_cli_deletes_and_restores(cli, tmp_path):
    g = gname()
    cli("groups", "create", g, "--template", "research-team")
    spec = tmp_path / "one.yaml"
    spec.write_text(f"""
apiVersion: pool/v1alpha1
kind: Job
metadata: {{name: one, group: {g}}}
spec:
  image: {IMG}
  command: ["sh", "-c", "echo hi"]
""")
    job_id = cli("submit", str(spec)).output.strip().split("id=")[1].split()[0]
    assert "job deleted" in cli("jobs", "delete", job_id, "--yes").output
    assert "restore it before" in cli("groups", "delete", g, "--yes").output
    assert g in cli("groups", "deleted").output
    assert "restored" in cli("groups", "restore", g).output
    assert g in cli("groups", "list").output


async def test_cli_stores_a_registry_token_from_stdin(cli, base_url, tmp_path):
    from test_private_images import SECRET, USER, FakeRegistry
    from typer.testing import CliRunner

    reg = FakeRegistry()
    try:
        g = gname()
        cli("groups", "create", g, "--template", "research-team")
        res = CliRunner().invoke(app, ["registry", "set", reg.host, "--username", USER, "--group", g, "--token-stdin"],
                                 input=SECRET + "\n", catch_exceptions=False)
        assert res.exit_code == 0 and "stored encrypted" in res.output, res.output
        listed = cli("registry", "list", g).output
        assert reg.host in listed and USER in listed and SECRET not in listed
        assert "removed" in cli("registry", "remove", reg.host, "--group", g).output
    finally:
        reg.close()


async def test_cli_short_job_files_sweep_and_my_tokens(cli, tmp_path):
    from test_private_images import SECRET, USER, FakeRegistry

    g = gname()
    cli("groups", "create", g)
    spec = tmp_path / "short.yaml"
    spec.write_text(textwrap.dedent(f"""\
        image: {IMG}
        command: python sim.py --seed {{{{seed}}}}
        sweep:
          seed: 0..2
        memory: 256Mi
    """))
    assert cli("sweep", str(spec), "--set", "seed=0..9", "--dry-run").output.startswith("10 tasks")
    assert "3 task(s)" in cli("submit", str(spec), "--group", g).output
    reg = FakeRegistry()
    try:
        res = CliRunner().invoke(app, ["registry", "set", reg.host, "--username", USER, "--mine", "--token-stdin"],
                                 input=SECRET + "\n")
        assert res.exit_code == 0 and "for your own jobs" in res.output, res.output
        assert reg.host in cli("registry", "list", "--mine").output
        assert "removed" in cli("registry", "remove", reg.host, "--mine").output
    finally:
        reg.close()
