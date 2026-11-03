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

from pool_protocol.jobspec import JobSpecError, load_job_spec

from .config import Config

app = typer.Typer(help="pool: run jobs on your group's shared computers.", no_args_is_help=True)
groups_app = typer.Typer(help="Groups you belong to.", no_args_is_help=True)
members_app = typer.Typer(help="Group members and roles.", no_args_is_help=True)
invite_app = typer.Typer(help="Invite links.", no_args_is_help=True)
requests_app = typer.Typer(help="Join requests and votes.", no_args_is_help=True)
devices_app = typer.Typer(help="Your devices: enroll, share, revoke, history.", no_args_is_help=True)
jobs_app = typer.Typer(help="Jobs in a group.", no_args_is_help=True)
app.add_typer(groups_app, name="groups")
app.add_typer(members_app, name="members")
app.add_typer(invite_app, name="invite")
app.add_typer(requests_app, name="requests")
app.add_typer(devices_app, name="devices")
app.add_typer(jobs_app, name="jobs")

TERMINAL = {"succeeded", "failed", "cancelled"}


class ApiError(Exception):
    pass


def _client(cfg: Config | None = None) -> tuple[httpx.Client, Config]:
    cfg = cfg or Config.load()
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    return httpx.Client(base_url=cfg.server, headers=headers, timeout=30), cfg


def api(method: str, path: str, **kw) -> Any:
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
    return job


@app.command()
def submit(file: Path = typer.Argument(..., exists=True, readable=True), group: str = typer.Option(None),
           wait: bool = typer.Option(False, help="Wait until the job finishes")) -> None:
    """Submit a job spec (YAML, plan §6.5)."""
    job = _submit(_load_spec(file), group)
    if wait:
        _wait(job["id"])


def _parse_values(text: str) -> list:
    """'0..9' -> [0..9], 'a,b,c' -> ['a','b','c'], numbers become ints/floats."""
    if ".." in text and "," not in text:
        lo, hi = text.split("..", 1)
        return list(range(int(lo), int(hi) + 1))
    out: list = []
    for v in text.split(","):
        v = v.strip()
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


@app.command()
def sweep(file: Path = typer.Argument(..., exists=True), set_: list[str] = typer.Option(
        [], "--set", help="key=values, e.g. --set seed=0..9 --set dataset=imdb,ag_news"),
        group: str = typer.Option(None), dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Submit an array job, overriding matrix values from the command line."""
    data = _load_spec(file)
    matrix = dict(data["spec"].get("matrix") or {})
    for item in set_:
        key, _, values = item.partition("=")
        if not key or not values:
            raise ApiError(f"bad --set {item!r}; use key=values")
        matrix[key] = _parse_values(values)
    data["spec"]["matrix"] = matrix
    data["spec"]["shape"] = "array"
    count = len(list(itertools.product(*matrix.values()))) if matrix else 0
    if dry_run:
        load_job_spec(data)
        typer.echo(f"{count} tasks: {json.dumps(matrix)}")
        return
    _submit(data, group)


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
    table(api("GET", f"/api/v1/jobs/{job_id}/tasks"), ["index", "params", "state", "epoch", "attempts", "device"])


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


@app.command()
def download(job_id: str, task: int = typer.Option(0, help="Task index"),
             path: str = typer.Option(None, help="One output file (default: all of the task's outputs)"),
             out: Path = typer.Option(Path("."), "--out", "-o", help="Folder to save into")) -> None:
    """Download a finished task's output files and check their SHA-256."""
    tasks = api("GET", f"/api/v1/jobs/{job_id}/tasks")
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
        link = api("GET", f"/api/v1/jobs/{job_id}/tasks/{task}/download", params={"path": o["path"]})
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
        typer.echo(f"saved {target} ({o['size']} bytes, sha256 ok)")


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


def main() -> None:
    try:
        app()
    except ApiError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
