"""A job's results in one place (docs/user-guide.md §7): every task's files and log as one zip, and the
`result.json` files of all tasks as one table (CSV).

SECURITY-SENSITIVE: access control for job outputs (group members only, like single downloads) and
reading stored files. Needs security review (plan §8.10).

Files come from the object store and are checked against the SHA-256 the computer reported; a file that is
missing or does not match is left out and listed in the zip's README.txt. Sizes are capped so one request
cannot tie up the server: the zip at settings.results_zip_max_mb (it streams to the client, never held in
full), each result.json at 256 KiB, each CSV merged at 64 MiB per task and 256 MiB in all.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import queue
import threading
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pool_protocol.jobspec import result_folder

from .. import junit, storage
from ..auth import Principal, current_principal, get_session
from ..authz import MANAGERS, not_found
from ..models import Attempt, Device, Job, Task, TaskLog
from .jobs import _load_job

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["results"])

RESULT_FILE = "result.json"
MAX_RESULT_BYTES = 256 * 1024
MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_TABLE_TASKS = 5000
MAX_ZIP_LOG_BYTES = 64 * 1024 * 1024
MAX_CHECKED_BYTES = 64 * 1024 * 1024  # files up to this size are checked before they go into the zip
MAX_MERGED_CSV_BYTES = 256 * 1024 * 1024


class _Row:
    def __init__(self, task: Task, attempt_id: uuid.UUID | None, device: str | None, started: datetime | None,
                 ended: datetime | None, attempt_outputs: list | None = None, usage: dict | None = None):
        self.task, self.attempt_id, self.device, self.started, self.ended = task, attempt_id, device, started, ended
        self.attempt_outputs, self.usage = attempt_outputs or [], usage

    @property
    def outputs(self) -> list[dict[str, Any]]:
        return list((self.task.result or {}).get("outputs") or []) if self.task.state == "succeeded" else []

    @property
    def report_outputs(self) -> list[dict[str, Any]]:
        """Files of the latest run, also when it failed: a failing test suite is exactly the report you want."""
        return self.outputs or list(self.attempt_outputs)

    @property
    def seconds(self) -> float | None:
        return round((self.ended - self.started).total_seconds(), 1) if self.started and self.ended else None


async def _rows(s: AsyncSession, job: Job) -> list[_Row]:
    """Every task with the computer and times of its latest run (attempt that started)."""
    runs = (
        select(Attempt.task_id, Attempt.id, Attempt.device_id, Attempt.started_at, Attempt.ended_at,
               Attempt.outputs, Attempt.usage,
               func.row_number().over(partition_by=Attempt.task_id, order_by=Attempt.epoch.desc()).label("n"))
        .join(Task, Task.id == Attempt.task_id)
        .where(Task.job_id == job.id, Attempt.started_at.is_not(None))
    ).subquery()
    latest = select(runs).where(runs.c.n == 1).subquery()
    rows = (await s.execute(
        select(Task, latest.c.id, Device.name, latest.c.started_at, latest.c.ended_at, latest.c.outputs,
               latest.c.usage)
        .outerjoin(latest, latest.c.task_id == Task.id)
        .outerjoin(Device, Device.id == latest.c.device_id)
        .where(Task.job_id == job.id).order_by(Task.index)
    )).all()
    return [_Row(t, a, d, start, end, outs, usage) for t, a, d, start, end, outs, usage in rows]


MAX_REPORT_FILES = 500  # files read for one report (each read counts, also one that fails)
MAX_REPORT_READ_BYTES = 64 * 1024 * 1024  # bytes read for one report
REPORT_BUILDS = 2  # reports built at once on this server
REPORT_CACHE = 64  # reports kept
_build_slots = threading.BoundedSemaphore(REPORT_BUILDS)
_cache: OrderedDict[str, junit.Report] = OrderedDict()
_cache_lock = threading.Lock()


def _report_key(job: Job, rows: list[_Row]) -> str:
    """Changes whenever a task's latest run or its files change, so a kept report is never out of date."""
    h = hashlib.sha256(str(job.id).encode())
    for r in rows:
        h.update(f"{r.task.index}:{r.attempt_id}:{r.task.state}:".encode())
        h.update(",".join(str(o.get("sha256")) for o in r.report_outputs).encode())
    return h.hexdigest()


def _cached(key: str) -> junit.Report | None:
    with _cache_lock:
        report = _cache.get(key)
        if report is not None:
            _cache.move_to_end(key)
        return report


def _tests(settings, job: Job, rows: list[_Row]) -> junit.Report:  # noqa: ANN001
    """Every task's JUnit XML files, merged (docs/design/pool-decides.md §4.8). Runs in a thread.

    The files come from the tasks, so the work is bounded: one file in memory at a time, at most
    MAX_REPORT_FILES reads and MAX_REPORT_READ_BYTES in all, each stored file parsed once (tasks that wrote the
    same file share it), REPORT_BUILDS reports built at once, and a built report is kept until the job's
    tasks change."""
    key = _report_key(job, rows)
    if (report := _cached(key)) is not None:
        return report
    with _build_slots:
        if (report := _cached(key)) is not None:  # built by another request while this one waited
            return report
        report = junit.Report()
        parsed: dict[str, list[junit.Case] | None] = {}
        reads = read_bytes = skipped = 0
        for r in rows:
            for o in r.report_outputs:
                path, sha = str(o.get("path", "")), str(o.get("sha256", ""))
                if not junit.looks_like_junit(path, int(o.get("size") or 0)):
                    continue
                if sha not in parsed:
                    if reads >= MAX_REPORT_FILES or read_bytes >= MAX_REPORT_READ_BYTES:
                        skipped += 1
                        continue
                    reads += 1
                    data = _read_small(settings, job.group_id, o, junit.MAX_REPORT_BYTES)
                    read_bytes += len(data or b"")
                    parsed[sha] = junit.parse(data) if data is not None else None
                report.add(r.task.index, path, parsed[sha])
        if skipped:
            report.unreadable.insert(0, f"{skipped} more file(s) were not read: one report reads at most "
                                        f"{MAX_REPORT_FILES} files and {MAX_REPORT_READ_BYTES // (1024 * 1024)} MiB")
        with _cache_lock:
            _cache[key] = report
            while len(_cache) > REPORT_CACHE:
                _cache.popitem(last=False)
        return report


def _summary_md(job: Job, rows: list[_Row], show_reasons: bool, tests: dict[str, Any] | None) -> str:
    """The run in a few lines (docs/design/pool-decides.md §4.8): what ran where, size given and used, why."""
    states: dict[str, int] = {}
    for r in rows:
        states[r.task.state] = states.get(r.task.state, 0) + 1
    lines = [f"# {job.name}", "", f"Image: {job.image}", f"State: {job.state} "
             f"({', '.join(f'{v} {k}' for k, v in sorted(states.items()))})", "",
             f"Each task got: {job.cpu:g} CPU, {job.memory_mb} MiB memory, {job.disk_mb} MiB disk, "
             f"time limit {job.max_runtime_s or '-'} s.", ""]
    sizing = job.sizing or {}
    if show_reasons and sizing.get("reasons"):
        lines += ["## How the sizes were chosen", ""] + [f"- {r}" for r in sizing["reasons"]] + [""]
    if tests and tests.get("tests"):
        lines += [f"Tests: {tests['passed']} passed, {tests['failed']} failed, {tests['error']} errors, "
                  f"{tests['skipped']} skipped (tests.md).", ""]
    lines += ["## Tasks", "", "| Task | State | Computer | Tries | Seconds | Memory used (MiB) | CPU used |",
              "|---|---|---|---|---|---|---|"]
    for r in rows[:MAX_TABLE_TASKS]:
        u = r.usage or {}
        cpu = "" if u.get("avg_cpu") is None else f"{u['avg_cpu']:g}"
        lines.append(f"| {r.task.index} | {r.task.state} | {junit.cell(r.device) or '-'} | {r.task.attempts_count} | "
                     f"{r.seconds if r.seconds is not None else '-'} | {u.get('peak_memory_mb', '-')} | {cpu or '-'} |")
    failed = [r for r in rows if r.task.state == "failed"]
    if failed:
        lines += ["", "## Failed tasks", ""]
        for r in failed[:200]:
            res = r.task.result or {}
            why = res.get("error") or res.get("reason") or f"exit {res.get('exit_code', '?')}"
            lines.append(f"- Task {r.task.index}: {junit.cell(why)[:300]}")  # from the computer: one plain line
    return "\n".join(lines) + "\n"


class TestReport(BaseModel):
    files: int
    tests: int
    passed: int
    failed: int
    error: int
    skipped: int
    seconds: float
    failures: list[dict[str, Any]]
    more_failures: int
    slowest: list[dict[str, Any]]
    unreadable: list[str]


@router.get("/jobs/{job_id}/tests", response_model=TestReport)
async def test_report(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                      s: AsyncSession = Depends(get_session)):
    """Every task's JUnit XML report merged into one (group members only)."""
    job, _, _ = await _load_job(s, job_id, p)
    _need_store(request)
    rows = await _rows(s, job)
    report = _cached(_report_key(job, rows))
    if report is None:
        async with _async_slots(request):  # waiting requests do not hold the server's worker threads
            report = await asyncio.to_thread(_tests, request.app.state.settings, job, rows)
    return TestReport(**report.summary())


def _async_slots(request: Request) -> asyncio.Semaphore:
    state = request.app.state
    if not hasattr(state, "report_slots"):
        state.report_slots = asyncio.Semaphore(REPORT_BUILDS)
    return state.report_slots


def _need_store(request: Request) -> None:
    if not storage.enabled(request.app.state.settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "This server has no file store, so output files stay on the "
                                                      "computers that ran the tasks.")


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """{"a": {"b": 1}, "c": [1, 2]} -> {"a.b": 1, "c": "[1, 2]"}: one column per value."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    if isinstance(value, list):
        value = json.dumps(value)
    return {prefix[:-1] or "value": value}


def _read_small(settings, group_id: uuid.UUID, output: dict[str, Any], limit: int) -> bytes | None:  # noqa: ANN001
    if output.get("size", 0) > limit:
        return None
    buf = io.BytesIO()
    try:
        storage.copy_object(settings, group_id, output["sha256"], buf, limit)
    except (LookupError, ValueError):
        return None
    return buf.getvalue()


def _table(settings, job: Job, rows: list[_Row]) -> tuple[list[str], list[dict[str, Any]], int]:  # noqa: ANN001
    """Columns, one dict per task that wrote a readable result.json, and how many tasks did not."""
    params = list(dict.fromkeys(k for r in rows for k in (r.task.params or {})))
    fields: dict[str, None] = {}
    table: list[dict[str, Any]] = []
    missing = 0
    for r in rows[:MAX_TABLE_TASKS]:
        out = next((o for o in r.outputs if o.get("path") == RESULT_FILE), None)
        raw = _read_small(settings, job.group_id, out, MAX_RESULT_BYTES) if out else None
        try:
            data = json.loads(raw) if raw is not None else None
        except ValueError:
            data = None
        if data is None:
            missing += 1
            continue
        flat = _flatten(data) if isinstance(data, dict) else {"value": json.dumps(data)}
        fields.update(dict.fromkeys(k for k in flat if k not in params and k != "task"))
        table.append({"task": r.task.index, **(r.task.params or {}), **{k: v for k, v in flat.items() if k != "task"}})
    missing += max(0, len(rows) - MAX_TABLE_TASKS)
    return ["task", *params, *fields], table, missing


def _csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


class ResultsTable(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    missing: int  # tasks without a readable result.json (not finished, failed, or none written)


@router.get("/jobs/{job_id}/results", response_model=ResultsTable)
async def results_table(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """Every task's /out/result.json as one table, with the task's sweep values (group members only)."""
    job, _, _ = await _load_job(s, job_id, p)
    _need_store(request)
    rows = await _rows(s, job)
    columns, table, missing = await asyncio.to_thread(_table, request.app.state.settings, job, rows)
    return ResultsTable(columns=columns, rows=table, missing=missing)


def _attachment(name: str) -> dict[str, str]:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or "results"
    return {"Content-Disposition": f'attachment; filename="{safe}"'}


@router.get("/jobs/{job_id}/results.csv")
async def results_csv(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                      s: AsyncSession = Depends(get_session)):
    """The results table as CSV."""
    job, _, _ = await _load_job(s, job_id, p)
    _need_store(request)
    rows = await _rows(s, job)
    columns, table, _ = await asyncio.to_thread(_table, request.app.state.settings, job, rows)
    return Response(_csv(columns, table), media_type="text/csv", headers=_attachment(f"{job.name}-results.csv"))


async def _log_text(s: AsyncSession, task: Task) -> str:
    """The task's log (its latest run), up to MAX_ZIP_LOG_BYTES: logs are not capped per task yet."""
    attempt_id = (await s.execute(
        select(Attempt.id).where(Attempt.task_id == task.id, Attempt.started_at.is_not(None))
        .order_by(Attempt.epoch.desc()).limit(1)
    )).scalar()
    if attempt_id is None:
        return ""
    parts: list[str] = []
    size = 0
    result = await s.stream(select(TaskLog.stream, TaskLog.chunk).where(TaskLog.attempt_id == attempt_id)
                            .order_by(TaskLog.id))
    async for stream, chunk in result:
        size += len(chunk)
        if size > MAX_ZIP_LOG_BYTES:
            await result.close()
            parts.append(f"\n[log cut here: more than {MAX_ZIP_LOG_BYTES // (1024 * 1024)} MiB; "
                         "`pool logs` shows the rest]\n")
            break
        parts.append(chunk if stream in ("stdout", "stderr") else f"[{stream}] {chunk}")
    return "".join(parts)


@router.get("/jobs/{job_id}/tasks/{index}/log.txt", response_class=PlainTextResponse)
async def task_log_file(job_id: uuid.UUID, index: int, p: Principal = Depends(current_principal),
                        s: AsyncSession = Depends(get_session)):
    """The task's whole log (its latest run) as a text file."""
    job, _, _ = await _load_job(s, job_id, p)
    task = (await s.execute(select(Task).where(Task.job_id == job.id, Task.index == index))).scalar_one_or_none()
    if task is None:
        raise not_found("task")
    return PlainTextResponse(await _log_text(s, task), headers=_attachment(f"{job.name}-task-{index}-log.txt"))


def _merge_csvs(settings, job: Job, rows: list[_Row], z: zipfile.ZipFile, notes: list[str]) -> None:  # noqa: ANN001
    """CSV files with the same path and the same header in several tasks -> combined/<path>, one file with
    the task number and sweep values in front of every row."""
    params = list(dict.fromkeys(k for r in rows for k in (r.task.params or {})))
    paths = dict.fromkeys(o["path"] for r in rows for o in r.outputs if o["path"].lower().endswith(".csv"))
    for path in paths:
        header: list[str] | None = None
        buf = io.StringIO()
        out = csv.writer(buf, lineterminator="\n")
        used = 0
        for r in rows:
            o = next((o for o in r.outputs if o["path"] == path), None)
            raw = _read_small(settings, job.group_id, o, MAX_CSV_BYTES) if o else None
            if raw is None:
                continue
            reader = csv.reader(io.StringIO(raw.decode("utf-8", errors="replace")))
            first = next(reader, None)
            if first is None:
                continue
            if header is None:
                header = first
                out.writerow(["task", *params, *header])
            elif first != header:
                notes.append(f"combined/{path} was not made: the tasks' {path} files have different columns.")
                header = None
                break
            for line in reader:
                out.writerow([r.task.index, *[(r.task.params or {}).get(k, "") for k in params], *line])
            used += 1
            if buf.tell() > MAX_MERGED_CSV_BYTES:
                notes.append(f"combined/{path} was not made: more than {MAX_MERGED_CSV_BYTES // (1024 * 1024)} MiB "
                             "in all; the tasks' own files are in their folders.")
                header = None
                break
        if header is not None and used > 1:
            z.writestr(f"combined/{path}", buf.getvalue())


def _readme(job: Job, notes: list[str], has_table: bool) -> str:
    lines = [f"Results of job {job.name} ({job.id})", "",
             "task-NNN.../   one folder per task: the files it wrote to /out, and log.txt",
             "tasks.csv      every task: its values, state, tries, computer and run time"]
    if has_table:
        lines.append("results.csv    every task's result.json in one table")
    lines.append("combined/      CSV files of the same name from all tasks, merged (when their columns match)")
    lines.append("summary.md     the run: what ran where, the size given and used, failed tasks")
    lines.append("tests.md       every task's JUnit XML test report merged into one (when there are any)")
    if notes:  # file names come from the tasks: one line each
        lines += ["", "Not included:", *[f"- {junit.one_line(n, 1000)}" for n in notes]]
    return "\n".join(lines) + "\n"


async def _zip_logs(s: AsyncSession, rows: list[_Row]) -> tuple[dict[int, str], bool]:
    """Each task's log (its latest run) in one query, up to MAX_ZIP_LOG_BYTES in all. True if cut short."""
    by_attempt = {r.attempt_id: r.task.index for r in rows if r.attempt_id}
    parts: dict[int, list[str]] = {}
    size = 0
    result = await s.stream(
        select(TaskLog.attempt_id, TaskLog.stream, TaskLog.chunk)
        .where(TaskLog.attempt_id.in_(list(by_attempt))).order_by(TaskLog.attempt_id, TaskLog.id)
    ) if by_attempt else None
    if result is None:
        return {}, False
    async for attempt_id, stream, chunk in result:
        size += len(chunk)
        if size > MAX_ZIP_LOG_BYTES:
            await result.close()
            return {k: "".join(v) for k, v in parts.items()}, True
        parts.setdefault(by_attempt[attempt_id], []).append(
            chunk if stream in ("stdout", "stderr") else f"[{stream}] {chunk}")
    return {k: "".join(v) for k, v in parts.items()}, False


class _Pipe(io.RawIOBase):
    """A write-only file that hands its bytes to the HTTP response in 1 MiB chunks, so the zip is never held
    in full by the server (its /tmp may be memory). Blocks while the client is slow; stops if it goes away."""

    def __init__(self) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
        self.buf = bytearray()
        self.gone = threading.Event()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # noqa: ANN001
        self.buf += b
        if len(self.buf) >= 1024 * 1024:
            self._send(bytes(self.buf))
            self.buf.clear()
        return len(b)

    def _send(self, chunk: bytes | None) -> None:
        while True:
            if self.gone.is_set():
                raise BrokenPipeError("the client went away")
            try:
                self.q.put(chunk, timeout=1)
                return
            except queue.Full:
                continue

    def finish(self) -> None:
        if self.buf:
            self._send(bytes(self.buf))
            self.buf.clear()
        self._send(None)


def _add_file(settings, job: Job, z: zipfile.ZipFile, name: str, o: dict[str, Any], notes: list[str]) -> None:  # noqa: ANN001
    """One stored file into the zip. Files up to MAX_CHECKED_BYTES are checked in full first, so a damaged one
    is left out; larger ones are checked while they stream (the store already verified them on upload)."""
    limit = settings.results_zip_max_mb * 1024 * 1024
    try:
        if o.get("size", 0) <= MAX_CHECKED_BYTES:
            buf = io.BytesIO()
            storage.copy_object(settings, job.group_id, o["sha256"], buf, MAX_CHECKED_BYTES)
            z.writestr(name, buf.getvalue())
        else:
            with z.open(name, "w", force_zip64=True) as f:
                storage.copy_object(settings, job.group_id, o["sha256"], f, limit)
    except LookupError:
        notes.append(f"{name}: not in the file store (it was not uploaded)")
    except ValueError:
        notes.append(f"{name}: does not match the checksum the computer reported"
                     + (", so it was left out" if o.get("size", 0) <= MAX_CHECKED_BYTES else "; do not trust it"))


def _write_zip(settings, job: Job, rows: list[_Row], logs: dict[int, str], logs_cut: bool, out: _Pipe,  # noqa: ANN001
               show_reasons: bool = False) -> None:
    notes: list[str] = []
    if logs_cut:
        notes.append(f"some logs: more than {MAX_ZIP_LOG_BYTES // (1024 * 1024)} MiB of logs in all; the job page "
                     "and `pool logs` show each task's full log")
    count = len(rows)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        summary = io.StringIO()
        params = list(dict.fromkeys(k for r in rows for k in (r.task.params or {})))
        w = csv.writer(summary, lineterminator="\n")
        w.writerow(["task", *params, "state", "tries", "computer", "exit_code", "seconds", "files"])
        for r in rows:
            folder = result_folder(r.task.index, count, r.task.params or {})
            w.writerow([r.task.index, *[(r.task.params or {}).get(k, "") for k in params], r.task.state,
                        r.task.attempts_count, r.device or "", (r.task.result or {}).get("exit_code", ""),
                        r.seconds if r.seconds is not None else "", len(r.outputs)])
            if logs.get(r.task.index):
                z.writestr(f"{folder}/log.txt", logs[r.task.index])
            for o in r.outputs:
                rel = PurePosixPath(o["path"])
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    notes.append(f"{folder}: {o['path']!r} has an unusable name")
                    continue
                _add_file(settings, job, z, f"{folder}/{rel}", o, notes)
        z.writestr("tasks.csv", summary.getvalue())
        columns, table, _ = _table(settings, job, rows)
        if table:
            z.writestr("results.csv", _csv(columns, table))
        _merge_csvs(settings, job, rows, z, notes)
        try:
            tests = _tests(settings, job, rows).summary()
        except Exception:  # noqa: BLE001 (the task files and logs above matter more than the test report)
            log.exception("test report for job %s failed", job.id)
            notes.append("tests.md: the test report could not be built")
            tests = None
        if tests and tests["tests"]:
            z.writestr("tests.md", junit.markdown(tests))
            notes.extend(f"tests: {u} is not a JUnit report pool can read" for u in tests["unreadable"])
        try:
            z.writestr("summary.md", _summary_md(job, rows, show_reasons, tests))
        except Exception:  # noqa: BLE001
            log.exception("summary for job %s failed", job.id)
            notes.append("summary.md: the summary could not be built")
        z.writestr("README.txt", _readme(job, notes, bool(table)))
    out.finish()


def _next_chunk(pipe: _Pipe) -> bytes | None:
    """The next chunk, or None at the end. Wakes up every second, so it never outlives a stopped writer."""
    while True:
        try:
            return pipe.q.get(timeout=1)
        except queue.Empty:
            if pipe.gone.is_set():
                return None


async def _stream(pipe: _Pipe, errors: list[BaseException]) -> AsyncIterator[bytes]:
    try:
        while (chunk := await asyncio.to_thread(_next_chunk, pipe)) is not None:
            yield chunk
        if errors:
            raise RuntimeError("the results zip could not be completed") from errors[0]
    finally:
        pipe.gone.set()  # the client went away or the zip is done: let the writer thread stop


@router.get("/jobs/{job_id}/results.zip")
async def results_zip(job_id: uuid.UUID, request: Request, p: Principal = Depends(current_principal),
                      s: AsyncSession = Depends(get_session)):
    """Every task's output files and log, tasks.csv, results.csv and merged CSVs, as one zip."""
    job, _, role = await _load_job(s, job_id, p)
    _need_store(request)
    settings = request.app.state.settings
    rows = await _rows(s, job)
    show_reasons = job.submitter_id == p.user.id or role in MANAGERS
    total = sum(o.get("size", 0) for r in rows for o in r.outputs)
    if total > settings.results_zip_max_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                            f"The results are {total // (1024 * 1024)} MiB, more than this server puts in one zip "
                            f"({settings.results_zip_max_mb} MiB). Use `pool download {job.id} --all`.")
    logs, cut = await _zip_logs(s, rows)
    pipe, errors = _Pipe(), []

    def write() -> None:  # its own thread: a slow download must not hold one of the server's worker threads
        try:
            _write_zip(settings, job, rows, logs, cut, pipe, show_reasons)
        except BrokenPipeError:
            pass  # the client went away
        except Exception as exc:  # the response ends early; the client sees a broken download, not a wrong file
            log.exception("results zip for job %s failed", job.id)
            errors.append(exc)
            pipe.gone.set()

    threading.Thread(target=write, name=f"results-zip-{job.id}", daemon=True).start()
    return StreamingResponse(_stream(pipe, errors), media_type="application/zip",
                             headers=_attachment(f"{job.name}-results.zip"))
