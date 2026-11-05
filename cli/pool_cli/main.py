"""`pool` command-line client (plan §5.1): `pool submit job.yaml`, `pool logs`, `pool sweep`, ..."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml

from pool_protocol.jobspec import JobSpecError, is_short_form, load_job_spec, parse_values, result_folder

from .config import Config

app = typer.Typer(help="pool: run jobs on your group's shared computers.", no_args_is_help=True)
groups_app = typer.Typer(help="Groups you belong to.", no_args_is_help=True)
members_app = typer.Typer(help="Group members and roles.", no_args_is_help=True)
invite_app = typer.Typer(help="Invite links.", no_args_is_help=True)
requests_app = typer.Typer(help="Join requests and votes.", no_args_is_help=True)
devices_app = typer.Typer(help="Your devices: enroll, share, revoke, history.", no_args_is_help=True)
jobs_app = typer.Typer(help="Jobs in a group.", no_args_is_help=True)
registry_app = typer.Typer(help="Read-only registry tokens for a group's private images.", no_args_is_help=True)
app.add_typer(groups_app, name="groups")
app.add_typer(members_app, name="members")
app.add_typer(invite_app, name="invite")
app.add_typer(requests_app, name="requests")
app.add_typer(devices_app, name="devices")
app.add_typer(jobs_app, name="jobs")
app.add_typer(registry_app, name="registry")

TERMINAL = {"succeeded", "failed", "cancelled"}


class ApiError(Exception):
    pass


def _client(cfg: Config | None = None) -> tuple[httpx.Client, Config]:
    cfg = cfg or Config.load()
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    return httpx.Client(base_url=cfg.server, headers=headers, timeout=30), cfg


def api(method: str, path: str, raw: bool = False, **kw) -> Any:
    """Call the server; the JSON answer, or with raw=True the body as text."""
    client, _ = _client()
    with client:
        try:
            r = client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(f"cannot reach server: {exc}") from None
    if r.status_code == 401:
        raise ApiError("not logged in or token expired: run `pool login`")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise ApiError(f"{r.status_code}: {detail}")
    if raw:
        return r.text
    return r.json() if r.content else None


def table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    widths = {c: max(len(c), *(len(_fmt(r.get(c))) for r in rows)) for c in cols}
    typer.echo("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        typer.echo("  ".join(_fmt(r.get(c)).ljust(widths[c]) for c in cols))


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, dict):
        return ",".join(f"{k}={x}" for k, x in v.items())
    if isinstance(v, list):
        return ",".join(map(str, v))
    return str(v)


def _group(group: str | None) -> str:
    g = group or Config.load().default_group
    if not g:
        raise ApiError("no group given: pass --group or run `pool login --group NAME`")
    return g


# ------------------------------------------------------------------ account


@app.command()
def login(server: str = typer.Option(..., help="Control plane URL, e.g. https://pool.example.org"),
          token: str = typer.Option(None, help="OIDC access token (or set POOL_TOKEN)"),
          dev_user: str = typer.Option(None, help="DEV MODE servers only: get a token for this username"),
          group: str = typer.Option(None, help="Default group for later commands")) -> None:
    """Save the server URL and a token to ~/.config/pool/config.json (mode 0600)."""
    cfg = Config.load()
    cfg.server = server.rstrip("/")
    if dev_user:
        r = httpx.post(f"{cfg.server}/dev/token", json={"username": dev_user}, timeout=15)
        if r.status_code != 200:
            raise ApiError(f"dev login failed ({r.status_code}); is the server in dev mode?")
        cfg.token = r.json()["access_token"]
    elif token:
        cfg.token = token
    if group:
        cfg.default_group = group
    path = cfg.save()
    me = api("GET", "/api/v1/me")
    typer.echo(f"logged in as {me['handle']} on {cfg.server} (config: {path})")


@app.command()
def whoami() -> None:
    me = api("GET", "/api/v1/me")
    typer.echo(f"{me['handle']}  mfa={me['mfa']}  server_admin={me['server_admin']}")


# ------------------------------------------------------------------ groups


@groups_app.command("list")
def groups_list() -> None:
    table(api("GET", "/api/v1/groups"), ["name", "my_role", "approval_rule", "state", "expires_at"])


@groups_app.command("create")
def groups_create(name: str, display_name: str = typer.Option(None), template: str = typer.Option(
        None, help="research-team | dev-team | student-group | course | hackathon")) -> None:
    g = api("POST", "/api/v1/groups", json={"name": name, "display_name": display_name or name,
                                            "template": template})
    typer.echo(f"created group {g['name']} (rule {g['approval_rule']}, you are coordinator)")


@groups_app.command("show")
def groups_show(name: str) -> None:
    typer.echo(json.dumps(api("GET", f"/api/v1/groups/{name}"), indent=2))
    typer.echo("\ndevices shared with this group:")
    table(api("GET", f"/api/v1/groups/{name}/devices"), ["name", "owner", "os", "online", "active_now", "tiers"])


@groups_app.command("usage")
def groups_usage(name: str, days: int = 7) -> None:
    table(api("GET", f"/api/v1/groups/{name}/usage", params={"days": days}),
          ["member", "cpu_hours", "gpu_hours", "attempts"])


@groups_app.command("audit")
def groups_audit(name: str, limit: int = 50) -> None:
    rows = api("GET", f"/api/v1/groups/{name}/audit", params={"limit": 5000})[-limit:]
    table(rows, ["id", "ts", "actor", "action", "target_type", "target_id"])


@groups_app.command("close")
def groups_close(name: str, yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    if not yes:
        typer.confirm(f"Close group {name}? Running jobs stop; data is kept for the retention period.", abort=True)
    api("POST", f"/api/v1/groups/{name}/close", json={"confirm": True})
    typer.echo(f"group {name} closed")


@groups_app.command("delete")
def groups_delete(name: str, yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    """Delete a group (coordinators). It can be restored for 7 days, then its data is purged."""
    if not yes:
        typer.confirm(f"Delete group {name}? Its work stops now; it can be restored for 7 days.", abort=True)
    gone = api("DELETE", f"/api/v1/groups/{name}", json={"confirm_name": name})
    typer.echo(f"group {name} deleted; restore it before {gone['purge_after']} with: pool groups restore {name}")


@groups_app.command("deleted")
def groups_deleted() -> None:
    """Deleted groups you can still restore."""
    table(api("GET", "/api/v1/deleted-groups"), ["name", "display_name", "deleted_at", "purge_after"])


@groups_app.command("restore")
def groups_restore(name: str) -> None:
    api("POST", f"/api/v1/groups/{name}/restore")
    typer.echo(f"group {name} restored")


# ------------------------------------------------------------------ members / invites / requests


@members_app.command("list")
def members_list(group: str) -> None:
    table(api("GET", f"/api/v1/groups/{group}/members"), ["handle", "role", "probation_until", "joined_at"])


@members_app.command("role")
def members_role(group: str, handle: str, role: str) -> None:
    api("PATCH", f"/api/v1/groups/{group}/members/{handle}", json={"role": role})
    typer.echo(f"{handle} is now {role} in {group}")


@members_app.command("remove")
def members_remove(group: str, handle: str, yes: bool = typer.Option(False, "--yes")) -> None:
    if not yes:
        typer.confirm(f"Remove {handle} from {group}? Their jobs are cancelled and devices unshared.", abort=True)
    api("DELETE", f"/api/v1/groups/{group}/members/{handle}")
    typer.echo(f"removed {handle}")


@invite_app.command("create")
def invite_create(group: str, role: str = "member", max_uses: int = 1, hours: int = 72) -> None:
    inv = api("POST", f"/api/v1/groups/{group}/invites",
              json={"role": role, "max_uses": max_uses, "expires_in_hours": hours})
    typer.echo(f"invite token (shown once): {inv['token']}")
    typer.echo(f"accept with: pool invite accept {inv['token']}")


@invite_app.command("accept")
def invite_accept(token: str) -> None:
    g = api("POST", "/api/v1/invites/accept", json={"token": token})
    typer.echo(f"joined {g['name']} as {g['my_role']}")


@app.command()
def join(group: str, message: str = "") -> None:
    """Ask to join a group (follows the group's approval rule)."""
    jr = api("POST", f"/api/v1/groups/{group}/join-requests", json={"message": message})
    typer.echo(f"request {jr['id']} sent; needs {jr['needed']} approval(s)")


@requests_app.command("list")
def requests_list(group: str) -> None:
    table(api("GET", f"/api/v1/groups/{group}/join-requests"),
          ["id", "handle", "message", "approvals", "rejections", "needed"])


@requests_app.command("vote")
def requests_vote(group: str, request_id: str, decision: str = typer.Argument(..., help="approve | reject")) -> None:
    jr = api("POST", f"/api/v1/groups/{group}/join-requests/{request_id}/votes", json={"decision": decision})
    typer.echo(f"request is {jr['state']} ({jr['approvals']}/{jr['needed']} approvals)")


# ------------------------------------------------------------------ devices


@devices_app.command("list")
def devices_list() -> None:
    table(api("GET", "/api/v1/devices"), ["id", "name", "os", "state", "online", "shared_with", "cert_not_after"])


@devices_app.command("code")
def devices_code() -> None:
    """Create a one-time enrollment code (valid 10 minutes)."""
    c = api("POST", "/api/v1/devices/enrollment-codes")
    server = Config.load().server
    typer.echo(f"code: {c['code']}  (expires {c['expires_at']})")
    typer.echo(f"on the device run: pool-agent enroll --server {server} --code {c['code']}")


@devices_app.command("share")
def devices_share(device_id: str, group: str) -> None:
    d = api("PUT", f"/api/v1/devices/{device_id}/shares/{group}")
    typer.echo(f"{d['name']} shared with: {', '.join(d['shared_with'])}")
    typer.echo("remember: the agent's local policy.yaml must also list this group")


@devices_app.command("unshare")
def devices_unshare(device_id: str, group: str) -> None:
    d = api("DELETE", f"/api/v1/devices/{device_id}/shares/{group}")
    typer.echo(f"{d['name']} shared with: {', '.join(d['shared_with']) or '(nothing)'}")


@devices_app.command("revoke")
def devices_revoke(device_id: str, yes: bool = typer.Option(False, "--yes")) -> None:
    if not yes:
        typer.confirm(f"Revoke device {device_id}? It must be enrolled again to rejoin.", abort=True)
    d = api("POST", f"/api/v1/devices/{device_id}/revoke")
    typer.echo(f"{d['name']} revoked")


@devices_app.command("history")
def devices_history(device_id: str, limit: int = 50) -> None:
    """What ran on this device, and who sent it."""
    table(api("GET", f"/api/v1/devices/{device_id}/history", params={"limit": limit}),
          ["created_at", "group", "submitter", "job_name", "task_index", "state", "exit_code", "image"])


# ------------------------------------------------------------------ jobs


def _load_spec(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ApiError(f"{path} is not a job spec")
    return data


def _submit(data: dict, group: str | None) -> dict:
    try:
        spec = load_job_spec(data)  # same validation as the server, for fast feedback
    except JobSpecError as exc:
        raise ApiError(f"invalid job spec: {exc}") from None
    g = group or spec.metadata.group or _group(None)
    job = api("POST", f"/api/v1/groups/{g}/jobs", json=data)
    typer.echo(f"submitted {job['name']} ({job['tasks']['total']} task(s)) id={job['id']}")
    for line in sizing_lines(job) + credit_lines(job):
        typer.echo(line)
    return job


def credit_lines(job: dict, after: bool = False) -> list[str]:
    """The cost of a job in credits (docs/design/credits.md §2), before it runs or after."""
    c = job.get("credits")
    if not c:
        return []
    if after:
        return [f"It cost {c['spent']:g} credits."] if c.get("spent") else []
    if c.get("expected") is not None:
        return [f"This run costs about {c['expected']:g} credits (at most {c['most']:g})."]
    return [f"This run costs at most {c['most']:g} credits; a first run is paid for by what it uses."]


def sizing_lines(job: dict, new_only: bool = False) -> list[str]:
    """Why each size was chosen (docs/design/pool-decides.md §4.9): pool's own choices, not the ones you set."""
    sizing = job.get("sizing") or {}
    reasons = sizing.get("reasons") or []
    if new_only:
        return list(reasons[sizing.get("decided", len(reasons)):])
    return [r for r in reasons[: sizing.get("decided", len(reasons))] if not r.endswith(": as you asked.")]


@app.command()
def submit(file: Path = typer.Argument(..., exists=True, readable=True), group: str = typer.Option(None),
           wait: bool = typer.Option(False, help="Wait until the job finishes")) -> None:
    """Submit a job file (YAML): the short form (image, command, parts or sweep, ...) or the full form."""
    job = _submit(_load_spec(file), group)
    if wait:
        _wait(job["id"])


@app.command()
def sweep(file: Path = typer.Argument(..., exists=True), set_: list[str] = typer.Option(
        [], "--set", help="key=values, e.g. --set seed=0..9 --set dataset=imdb,ag_news"),
        group: str = typer.Option(None), dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Submit a job with sweep values set or replaced from the command line."""
    data = _load_spec(file)
    short = is_short_form(data)
    target = data if short else data.setdefault("spec", {})
    key_name = "sweep" if short else "matrix"
    matrix = {k: parse_values(v) for k, v in (target.get(key_name) or target.get("matrix") or {}).items()}
    for item in set_:
        key, _, values = item.partition("=")
        if not key or not values:
            raise ApiError(f"bad --set {item!r}; use key=values")
        try:
            matrix[key] = parse_values(values)
        except JobSpecError as exc:
            raise ApiError(str(exc)) from None
    target.pop("matrix", None)
    target[key_name] = matrix
    target.pop("parts", None)
    if not short:
        target["shape"] = "array"
    count = len(list(itertools.product(*matrix.values()))) if matrix else 0
    if dry_run:
        try:
            load_job_spec(data)
        except JobSpecError as exc:
            raise ApiError(f"invalid job spec: {exc}") from None
        typer.echo(f"{count} tasks: {json.dumps(matrix)}")
        return
    _submit(data, group)


def _registries_path(group: str | None, mine: bool) -> str:
    return "/api/v1/me/registries" if mine else f"/api/v1/groups/{_group(group)}/registries"


@registry_app.command("list")
def registry_list(group: str = typer.Argument(None),
                  mine: bool = typer.Option(False, "--mine", help="My own tokens instead of the group's")) -> None:
    rows = api("GET", _registries_path(group, mine))
    cols = ["registry", "username", "updated_at"] if mine else ["registry", "username", "updated_by", "updated_at"]
    table(rows, cols)


@registry_app.command("set")
def registry_set(registry: str, username: str = typer.Option(..., help="The read-only account"),
                 group: str = typer.Option(None),
                 mine: bool = typer.Option(False, "--mine", help="Store it for my own jobs only, in every group"),
                 token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from standard input")
                 ) -> None:
    """Store a read-only token so computers can pull private images: the group's (coordinators, admins), or
    with --mine your own, used only for jobs you submit. The token is never taken as a command-line argument,
    where it would end up in shell history."""
    token = sys.stdin.readline().strip() if token_stdin else typer.prompt("Read-only token", hide_input=True)
    r = api("PUT", _registries_path(group, mine), json={"registry": registry, "username": username, "token": token})
    whose = "for your own jobs" if mine else "for the group"
    typer.echo(f"saved {whose}: {r['registry']} as {r['username']} (checked with the registry, stored encrypted)")


@registry_app.command("remove")
def registry_remove(registry: str, group: str = typer.Option(None),
                    mine: bool = typer.Option(False, "--mine", help="Remove my own token")) -> None:
    api("DELETE", f"{_registries_path(group, mine)}/{registry}")
    typer.echo(f"removed the token for {registry}")


@jobs_app.command("delete")
def jobs_delete(job_id: str, yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    """Delete a job: its tasks, logs and output files (running work stops first)."""
    if not yes:
        typer.confirm(f"Delete job {job_id}? Its logs and output files are deleted for good.", abort=True)
    r = api("DELETE", f"/api/v1/jobs/{job_id}")
    typer.echo(f"job deleted ({r['files_deleted']} stored files removed)")


@jobs_app.command("list")
def jobs_list(group: str = typer.Argument(None), state: str = typer.Option(None)) -> None:
    params = {"state": state} if state else {}
    table(api("GET", f"/api/v1/groups/{_group(group)}/jobs", params=params),
          ["id", "name", "shape", "submitter", "state", "tasks", "created_at"])


@app.command()
def status(job_id: str) -> None:
    job = api("GET", f"/api/v1/jobs/{job_id}")
    typer.echo(f"{job['name']}  state={job['state']}  tasks={_fmt(job['tasks'])}")
    rows = api("GET", f"/api/v1/jobs/{job_id}/tasks")
    for r in rows:
        u = r.get("usage") or {}
        cpu = f",cpu={u['avg_cpu']:g}" if u.get("avg_cpu") is not None else ""
        r["used"] = f"{u['peak_memory_mb']}MiB{cpu},{u['wall_seconds']:.0f}s" if u.get("samples") else None
    table(rows, ["index", "params", "state", "epoch", "attempts", "device", "used"])


@app.command()
def logs(job_id: str, task: int = typer.Option(0, help="Task index"),
         follow: bool = typer.Option(False, "-f", "--follow", help="Keep printing until the task ends")) -> None:
    """Print a task's logs (latest attempt)."""
    after = 0
    while True:
        rows = api("GET", f"/api/v1/jobs/{job_id}/tasks/{task}/logs", params={"after_id": after}) or []
        for r in rows:
            sys.stdout.write(r["chunk"])
            after = max(after, r["id"])
        sys.stdout.flush()
        if not follow:
            return
        if not rows:
            tasks = api("GET", f"/api/v1/jobs/{job_id}/tasks")
            if task >= len(tasks) or tasks[task]["state"] in TERMINAL:
                return
            time.sleep(2)


# SECURITY-SENSITIVE: input handling of untrusted paths. Needs security review (plan §8.10).
def safe_target(out_dir: Path, rel: str) -> Path:
    """Where to save an output file. Output paths come from the computer that ran the task, so they are
    untrusted: refuse absolute paths and anything that would leave out_dir."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ApiError(f"refusing unsafe output path {rel!r}")
    base = out_dir.resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise ApiError(f"refusing unsafe output path {rel!r}")
    return target


def _fetch(job_id: str, index: int, o: dict, target: Path) -> None:
    """Download one output file to target, keeping it only if its SHA-256 matches what the computer reported."""
    link = api("GET", f"/api/v1/jobs/{job_id}/tasks/{index}/download", params={"path": o["path"]})
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    with httpx.stream("GET", link["url"], timeout=60) as r:
        if r.status_code != 200:
            raise ApiError(f"download of {o['path']} failed: HTTP {r.status_code}")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                digest.update(chunk)
                f.write(chunk)
    if digest.hexdigest() != o["sha256"]:
        tmp.unlink(missing_ok=True)
        raise ApiError(f"{o['path']}: checksum does not match; file not saved")
    tmp.replace(target)


@app.command()
def download(job_id: str, task: int = typer.Option(0, help="Task index"),
             path: str = typer.Option(None, help="One output file (default: all of the task's outputs)"),
             all_tasks: bool = typer.Option(False, "--all", help="Every task's files, one folder per task"),
             out: Path = typer.Option(Path("."), "--out", "-o", help="Folder to save into")) -> None:
    """Download output files and check their SHA-256: one task's, or with --all every task's, each in its own
    folder (task-000, or task-000_seed=0 for a sweep), the same layout as Download all in the browser."""
    tasks = api("GET", f"/api/v1/jobs/{job_id}/tasks")
    if all_tasks:
        saved = 0
        for row in tasks:
            folder = out / result_folder(row["index"], len(tasks), row.get("params") or {})
            for o in (row.get("result") or {}).get("outputs") or []:
                if path and o["path"] != path:
                    continue
                _fetch(job_id, row["index"], o, safe_target(folder, o["path"]))
                saved += 1
        typer.echo(f"saved {saved} file(s) from {len(tasks)} task(s) under {out} (sha256 ok)")
        return
    row = next((t for t in tasks if t["index"] == task), None)
    if row is None:
        raise ApiError(f"job has no task {task}")
    outputs = (row.get("result") or {}).get("outputs") or []
    if path:
        outputs = [o for o in outputs if o["path"] == path]
    if not outputs:
        raise ApiError("no output files for that task" + (f" named {path!r}" if path else " yet"))
    for o in outputs:
        target = safe_target(out, o["path"])
        _fetch(job_id, task, o, target)
        typer.echo(f"saved {target} ({o['size']} bytes, sha256 ok)")


@app.command()
def results(job_id: str, out: Path = typer.Option(None, "--out", "-o", help="Save as this CSV file")) -> None:
    """Every task's result.json in one table (CSV), with the task's sweep values."""
    text = api("GET", f"/api/v1/jobs/{job_id}/results.csv", raw=True)
    if out:
        out.write_text(text)
        typer.echo(f"saved {out} ({max(text.count(chr(10)) - 1, 0)} row(s))")
    else:
        typer.echo(text, nl=False)


def test_lines(report: dict) -> list[str]:
    """A merged JUnit report in a few lines (docs/design/pool-decides.md §4.8)."""
    if not report or not report.get("tests"):
        return []
    lines = [f"Tests: {report['passed']} passed, {report['failed']} failed, {report['error']} errors, "
             f"{report['skipped']} skipped ({report['files']} report(s))."]
    for f in report.get("failures", [])[:20]:
        lines.append(f"  task {f['task']}: {f['suite']}::{f['name']}  {f['message'][:120]}".rstrip())
    more = len(report.get("failures", [])) - 20 + report.get("more_failures", 0)
    if more > 0:
        lines.append(f"  ...and {more} more")
    return lines


@app.command()
def tests(job_id: str) -> None:
    """Every task's JUnit XML test report (junit.xml in /out) merged into one: passed, failed, slowest."""
    report = api("GET", f"/api/v1/jobs/{job_id}/tests")
    if not report.get("tests"):
        typer.echo("no test reports: write JUnit XML to /out (e.g. pytest --junitxml=/out/junit.xml)")
        return
    for line in test_lines(report):
        typer.echo(line)
    if report.get("slowest"):
        typer.echo("Slowest:")
        for c in report["slowest"][:5]:
            typer.echo(f"  {c['seconds']:>8.2f} s  task {c['task']}: {c['suite']}::{c['name']}")


@app.command()
def cancel(job_id: str) -> None:
    job = api("POST", f"/api/v1/jobs/{job_id}/cancel")
    typer.echo(f"{job['name']} is {job['state']}")


def _wait(job_id: str, poll: float = 2.0) -> None:
    while True:
        job = api("GET", f"/api/v1/jobs/{job_id}")
        typer.echo(f"\r{job['state']:<10} {_fmt(job['tasks'])}", nl=False)
        if job["state"] in TERMINAL:
            typer.echo()
            if job["state"] != "succeeded":
                raise typer.Exit(1)
            return
        time.sleep(poll)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True}, add_help_option=False)
def run(ctx: typer.Context) -> None:
    """Run a program on a computer of your group, like `docker run` (pool run --help)."""
    from . import run as run_cmd

    # The `pool` command calls run_cmd.run with the raw arguments before Typer sees them (main below): Click
    # drops `--`, which `pool run [OPTIONS] -- ARG...` needs. This is only reached from inside Python.
    raise typer.Exit(run_cmd.run(list(ctx.args)))


@app.command()
def attach(job_id: str,
           out: Path = typer.Option(Path("pool-out"), "--out", "-o", help="Where the job's /out files go"),
           yes: bool = typer.Option(False, "--yes", "-y", help="Ctrl-C stops the job without asking")) -> None:
    """Follow a job: its output (or progress), then save its /out files. Works for any job, also one
    submitted in the browser."""
    from . import run as run_cmd

    raise typer.Exit(run_cmd.attach(job_id, out, yes=yes))


def main() -> None:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "run":
            from . import run as run_cmd

            raise SystemExit(run_cmd.run(sys.argv[2:]))
        app()
    except ApiError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------ credits (docs/design/credits.md)

credits_app = typer.Typer(help="Credits: earn by sharing your computer, spend by running jobs.",
                          invoke_without_command=True, no_args_is_help=False)
app.add_typer(credits_app, name="credits")


def _amount(v: float) -> str:
    return f"{v:g}"


@credits_app.callback()
def credits_balance(ctx: typer.Context, group: str = typer.Option(None, help="Group (default: from pool login)")):
    """Your credits in a group."""
    if ctx.invoked_subcommand is not None:
        return
    c = api("GET", f"/api/v1/groups/{_group(group)}/credits")
    if not c["enabled"]:
        typer.echo(f"{c['group']} does not use credits.")
        return
    typer.echo(f"{c['group']}: {_amount(c['balance'])} credits ({_amount(c['earned'])} earned, "
               f"{_amount(c['free'])} free)")
    if c.get("open_request"):
        r = c["open_request"]
        typer.echo(f"open request: {_amount(r['amount'])} credits ({r['reason']})")


@credits_app.command("history")
def credits_history(group: str = typer.Option(None), limit: int = typer.Option(30)) -> None:
    """Your last changes: earned, spent, given, received, free, moved."""
    rows = api("GET", f"/api/v1/groups/{_group(group)}/credits/history", params={"limit": limit})
    for r in rows:
        r["with"] = r.get("counterparty") or r.get("device")
    table(rows, ["created_at", "kind", "amount", "with", "note"])


@credits_app.command("give")
def credits_give(to: str, amount: float, note: str = typer.Option(None), group: str = typer.Option(None)) -> None:
    """Give credits to another member of the group."""
    c = api("POST", f"/api/v1/groups/{_group(group)}/credits/give", json={"to": to, "amount": amount, "note": note})
    typer.echo(f"gave {_amount(amount)} credits to {to}; you have {_amount(c['balance'])}")


@credits_app.command("ask")
def credits_ask(amount: float, reason: str = typer.Option(..., help="Why you need them"),
                group: str = typer.Option(None)) -> None:
    """Ask the coordinator for free credits."""
    g = _group(group)
    api("POST", f"/api/v1/groups/{g}/credits/requests", json={"amount": amount, "reason": reason})
    typer.echo(f"asked the coordinator of {g} for {_amount(amount)} credits")


@credits_app.command("requests")
def credits_requests(group: str = typer.Option(None), all_: bool = typer.Option(False, "--all")) -> None:
    """Open requests (the coordinator sees everyone's; you see your own)."""
    rows = api("GET", f"/api/v1/groups/{_group(group)}/credits/requests",
               params={"state": "all" if all_ else "open"})
    table(rows, ["id", "member", "amount", "reason", "state", "granted", "created_at"])


@credits_app.command("approve")
def credits_approve(request_id: str, amount: float = typer.Argument(None, help="Less than asked: approve part"),
                    group: str = typer.Option(None)) -> None:
    """Approve a request (coordinator)."""
    r = api("POST", f"/api/v1/groups/{_group(group)}/credits/requests/{request_id}/approve",
            json={"amount": amount} if amount is not None else {})
    typer.echo(f"approved {_amount(r['granted'])} credits for {r['member']}")


@credits_app.command("decline")
def credits_decline(request_id: str, group: str = typer.Option(None)) -> None:
    """Decline a request (coordinator)."""
    r = api("POST", f"/api/v1/groups/{_group(group)}/credits/requests/{request_id}/decline")
    typer.echo(f"declined the request of {r['member']}")


@credits_app.command("grant")
def credits_grant(to: str, amount: float, reason: str = typer.Option(...), group: str = typer.Option(None)) -> None:
    """Give a member free credits (coordinator; not to yourself)."""
    r = api("POST", f"/api/v1/groups/{_group(group)}/credits/grant",
            json={"to": to, "amount": amount, "reason": reason})
    typer.echo(f"granted {_amount(amount)} credits to {to}; they have {_amount(r['balance'])}")


@credits_app.command("members")
def credits_members(group: str = typer.Option(None)) -> None:
    """Every member's credits (coordinators and admins)."""
    table(api("GET", f"/api/v1/groups/{_group(group)}/credits/members"), ["member", "balance", "earned", "free"])


@credits_app.command("move")
def credits_move(amount: float, from_group: str = typer.Option(..., "--from"),
                 to_group: str = typer.Option(..., "--to")) -> None:
    """Move earned credits from one of your groups to another."""
    r = api("POST", "/api/v1/me/credits/move", json={"from_group": from_group, "to_group": to_group, "amount": amount})
    typer.echo(f"moved {_amount(amount)} credits: {_amount(r['source']['balance'])} in {from_group}, "
               f"{_amount(r['target']['balance'])} in {to_group}")

