"""One test report from many tasks (docs/design/pool-decides.md §4.8).

SECURITY-SENSITIVE: parses files written by tasks (untrusted). Needs security review (plan §8.10).

Most test tools write JUnit XML (pytest --junitxml, Maven, Gradle, Node's test runner, and others through
plugins). pool reads every task's .xml output that is a JUnit report and merges them: how many passed, failed,
errored or were skipped, each failure with its task and message, and the slowest tests.

The files come from the tasks, so they are untrusted:

- Only UTF-8 is read. The text is decoded strictly and fed to the parser as text, which makes the parser use
  UTF-8 whatever the file declares, so no other encoding can hide anything from the next check.
- A document with a DOCTYPE or an entity anywhere is refused. JUnit reports never need them.
- The parser streams: a file whose first element is not a test suite is dropped at once, each test is freed
  once read, and every test is counted once (in its nearest suite), so the work grows with the file's size.
- Sizes, elements and tests are capped, and any error only drops that one file.
"""

from __future__ import annotations

import codecs
import xml.etree.ElementTree as ET  # noqa: S405 (untrusted input: see the module docstring)
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

MAX_REPORT_BYTES = 10 * 1024 * 1024
MAX_ELEMENTS = 300_000  # per file
MAX_CASES = 50_000
MAX_LISTED = 200  # failures listed in full
MESSAGE_CHARS = 500
FEED_CHARS = 64 * 1024  # the parser gets the text in pieces, so other threads run in between
STATUS = {"failure": "failed", "error": "error", "skipped": "skipped"}


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

    def add(self, task: int, path: str, cases: list[Case] | None) -> None:
        """One file's tests (from parse), for this task. None: not a JUnit report pool reads."""
        if cases is None:
            if path.lower().endswith("junit.xml"):
                self.unreadable.append(f"task {task}: {one_line(path, 300)}")
            return
        self.files += 1
        room = MAX_CASES - len(self.cases)
        self.cases.extend(replace(c, task=task) for c in cases[:room])

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


def one_line(s: Any, n: int = MESSAGE_CHARS) -> str:
    """At most n characters on one line, without control or direction characters (they could fake output in a
    terminal). Cut before cleaning, so a huge value costs no more than a short one."""
    if s is None:
        return ""
    raw = str(s)[: n * 4]
    return " ".join("".join(ch if ch.isprintable() else " " for ch in raw).split())[:n]


def _decode(data: bytes) -> str | None:
    """The file as text when it is UTF-8 with no DOCTYPE or entity, else None."""
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]
    if b"\x00" in data:  # UTF-16 or UTF-32, or not text at all
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        return None
    return text


def _one(tc: ET.Element, suite: str) -> Case:
    try:
        seconds = float(tc.get("time") or 0)
    except ValueError:
        seconds = 0.0
    if not 0 <= seconds < float("inf"):  # also NaN
        seconds = 0.0
    status, message = "passed", ""
    for child in tc:
        if child.tag in STATUS:
            status, message = STATUS[child.tag], one_line(child.get("message") or child.text)
            break
    classname = one_line(tc.get("classname"), 300)
    return Case(task=0, suite=classname or suite, name=one_line(tc.get("name"), 300), status=status,
                seconds=round(seconds, 3), message=message)


def parse(data: bytes, task: int = 0) -> list[Case] | None:
    """The test cases in one file, or None when it is not a JUnit report (or not one pool reads)."""
    if len(data) > MAX_REPORT_BYTES:
        return None
    try:
        cases = _parse(data)
    except Exception:  # noqa: BLE001 (an untrusted file: whatever goes wrong, only this file is dropped)
        return None
    return cases if cases is None or not task else [replace(c, task=task) for c in cases]


class _Stop(Exception):  # noqa: N818 - control flow: enough read
    pass


def _parse(data: bytes) -> list[Case] | None:
    text = _decode(data)
    if text is None:
        return None
    parser = ET.XMLPullParser(events=("start", "end"))  # noqa: S314 (see the module docstring)
    cases: list[Case] = []
    suites: list[str] = []  # the open suites' names, innermost last
    elements = 0

    def take() -> None:
        nonlocal elements
        for event, el in parser.read_events():
            if event == "start":
                if elements == 0 and el.tag not in ("testsuites", "testsuite"):
                    raise _Stop  # not a JUnit report: no need to read further
                elements += 1
                if elements > MAX_ELEMENTS:
                    raise _Stop
                if el.tag == "testsuite":
                    suites.append(one_line(el.get("name"), 200))
            elif el.tag == "testcase":
                cases.append(_one(el, suites[-1] if suites else ""))
                el.clear()  # its failure text and output are not needed any more
                if len(cases) >= MAX_CASES:
                    raise _Stop
            elif el.tag == "testsuite":
                if suites:
                    suites.pop()
                el.clear()

    try:
        for at in range(0, len(text), FEED_CHARS):
            parser.feed(text[at:at + FEED_CHARS])  # text, not bytes: the parser reads it as UTF-8
            take()
        parser.close()
        take()  # the last events come with close()
    except _Stop:
        pass
    return cases if elements else None


def merge(files: Iterable[tuple[int, str, bytes]]) -> Report:
    """Merge (task, path, contents) files into one report. Files that are not JUnit reports are skipped. Give
    a generator to hold one file at a time."""
    report = Report()
    for task, path, data in files:
        report.add(task, path, parse(data))
    return report


def cell(s: Any) -> str:
    """Text for a Markdown table cell: one line, and nothing a Markdown viewer would turn into a link, an
    image or HTML."""
    out = one_line(s, 600)
    for ch in "\\`*_[]()<>|!#~":
        out = out.replace(ch, "\\" + ch)
    return out


def markdown(summary: dict[str, Any]) -> str:
    """The report as a short text for the zip. Names and messages come from the tasks: each is escaped."""
    lines = ["# Tests", "",
             f"{summary['tests']} tests in {summary['files']} report(s): {summary['passed']} passed, "
             f"{summary['failed']} failed, {summary['error']} errors, {summary['skipped']} skipped.", ""]
    if summary["failures"]:
        lines += ["## Failed", "", "| Task | Test | Message |", "|---|---|---|"]
        for f in summary["failures"]:
            lines.append(f"| {f['task']} | {cell(f['suite'])}::{cell(f['name'])} | {cell(f['message'])} |")
        if summary["more_failures"]:
            lines.append(f"\n…and {summary['more_failures']} more.")
        lines.append("")
    if summary["slowest"]:
        lines += ["## Slowest", "", "| Task | Test | Seconds |", "|---|---|---|"]
        lines += [f"| {c['task']} | {cell(c['suite'])}::{cell(c['name'])} | {c['seconds']} |"
                  for c in summary["slowest"]]
    return "\n".join(lines) + "\n"
