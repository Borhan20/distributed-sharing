"""Plain-language hints for programs that hit pool's container rules (docs/design/pool-run.md §2.0).

They match the operating system's own error texts, which programs in any language usually pass on (Go and
Node in lower case, so matching ignores case). The same hints show after `pool run --local` and after a run
on a member's computer.
"""

from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("read-only", re.compile(r"read-only file system|readonlyfilesystem|EROFS", re.I),
     "The container's file system is read-only. Write to /out (kept, comes back to you) or /tmp (scratch)."),
    ("network", re.compile(
        r"network is unreachable|temporary failure in name resolution|could not resolve host|"
        r"name or service not known|no such host|unknownhostexception|nodename nor servname|"
        r"getaddrinfo (?:failed|enotfound|eai_again)|failed to resolve", re.I),
     "Jobs have no network: no downloads or outside services while they run. Put dependencies, models and "
     "data into the image (in the Dockerfile)."),
    ("permission", re.compile(r"permission denied|operation not permitted|EACCES|EPERM", re.I),
     "The program runs as an unprivileged user (uid 65534), not root. Write under /out or /tmp; files it only "
     "reads must be readable by everyone (in the Dockerfile: chmod -R a+rX)."),
]


def match(text: str) -> list[str]:
    """The hints whose error text appears in `text`, each once, in a fixed order."""
    return [hint for _, pattern, hint in _RULES if pattern.search(text)]


def for_exit(code: int | None, memory: str | None) -> list[str]:
    if code == 137:
        limit = f" ({memory})" if memory else ""
        return [f"The program was stopped with signal 9, usually for using more than its memory limit{limit}. "
                "Ask for more with --memory."]
    return []
