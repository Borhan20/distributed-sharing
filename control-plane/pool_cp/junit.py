"""One test report from many tasks (docs/design/pool-decides.md §4.8).

Most test tools write JUnit XML (pytest --junitxml, Maven, Gradle, Node's test runner, and others through
plugins). pool reads every task's .xml output that is a JUnit report and merges them: how many passed, failed,
errored or were skipped, each failure with its task and message, and the slowest tests.

The files come from the tasks, so they are untrusted: a document with a DOCTYPE or entities is refused (JUnit
reports never need them; Python's parser does not fetch external entities, and its expat refuses entity
expansion attacks, but there is no reason to parse them at all), and sizes are capped.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 (untrusted input: DOCTYPE refused below, sizes capped)
from dataclasses import dataclass, field
from typing import Any

MAX_REPORT_BYTES = 10 * 1024 * 1024
MAX_CASES = 50_000
MAX_LISTED = 200  # failures listed in full
MESSAGE_CHARS = 500


@dataclass
class Case:
    task: int
    suite: str
    name: str
    status: str  # passed, failed, error, skipped
    seconds: float
    message: str = ""


@dataclass
class Report:
    cases: list[Case] = field(default_factory=list)
    files: int = 0
    unreadable: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts = {s: 0 for s in ("passed", "failed", "error", "skipped")}
        for c in self.cases:
            counts[c.status] += 1
        bad = [c for c in self.cases if c.status in ("failed", "error")]
        return {
            "files": self.files,
            "tests": len(self.cases),
            **counts,
            "seconds": round(sum(c.seconds for c in self.cases), 2),
            "failures": [_case(c) for c in bad[:MAX_LISTED]],
            "more_failures": max(0, len(bad) - MAX_LISTED),
            "slowest": [_case(c) for c in sorted(self.cases, key=lambda c: -c.seconds)[:10]],
            "unreadable": self.unreadable[:50],
        }


def _case(c: Case) -> dict[str, Any]:
    return {"task": c.task, "suite": c.suite, "name": c.name, "status": c.status, "seconds": c.seconds,
            "message": c.message}


def looks_like_junit(path: str, size: int) -> bool:
    return path.lower().endswith(".xml") and 0 < size <= MAX_REPORT_BYTES


def _text(s: Any, n: int = MESSAGE_CHARS) -> str:
    return "" if s is None else " ".join(str(s).split())[:n]


def parse(data: bytes, task: int) -> list[Case] | None:
    """The test cases in one file, or None when it is not a JUnit report (or not one pool reads)."""
    if len(data) > MAX_REPORT_BYTES:
        return None
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        return None
    try:
        root = ET.fromstring(data)  # noqa: S314 (see the module docstring)
    except (ET.ParseError, ValueError, RecursionError):
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    cases: list[Case] = []
    for suite in ([root] if root.tag == "testsuite" else root.iter("testsuite")):
        suite_name = _text(suite.get("name"), 200)
        for tc in suite.iter("testcase"):
            if len(cases) >= MAX_CASES:
                return cases
            try:
                seconds = max(0.0, float(tc.get("time") or 0))
            except ValueError:
                seconds = 0.0
            if seconds != seconds or seconds == float("inf"):
                seconds = 0.0
            status, message = "passed", ""
            for tag in ("failure", "error", "skipped"):
                el = tc.find(tag)
                if el is not None:
                    status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                    message = _text(el.get("message") or el.text)
                    break
            name = _text(tc.get("name"), 300)
            classname = _text(tc.get("classname"), 300)
            cases.append(Case(task=task, suite=classname or suite_name, name=name, status=status,
                              seconds=round(seconds, 3), message=message))
    return cases


def merge(files: list[tuple[int, str, bytes]]) -> Report:
    """Merge (task, path, contents) files into one report. Files that are not JUnit reports are skipped."""
    report = Report()
    for task, path, data in files:
        cases = parse(data, task)
        if cases is None:
            if path.lower().endswith("junit.xml"):
                report.unreadable.append(f"task {task}: {path}")
            continue
        report.files += 1
        room = MAX_CASES - len(report.cases)
        report.cases.extend(cases[:room])
    return report


def markdown(summary: dict[str, Any]) -> str:
    """The report as a short text for the zip."""
    lines = ["# Tests", "",
             f"{summary['tests']} tests in {summary['files']} report(s): {summary['passed']} passed, "
             f"{summary['failed']} failed, {summary['error']} errors, {summary['skipped']} skipped.", ""]
    if summary["failures"]:
        lines += ["## Failed", "", "| Task | Test | Message |", "|---|---|---|"]
        for f in summary["failures"]:
            msg = f["message"].replace("|", "\\|")
            lines.append(f"| {f['task']} | {f['suite']}::{f['name']} | {msg} |")
        if summary["more_failures"]:
            lines.append(f"\n…and {summary['more_failures']} more.")
        lines.append("")
    if summary["slowest"]:
        lines += ["## Slowest", "", "| Task | Test | Seconds |", "|---|---|---|"]
        lines += [f"| {c['task']} | {c['suite']}::{c['name']} | {c['seconds']} |" for c in summary["slowest"]]
    return "\n".join(lines) + "\n"
