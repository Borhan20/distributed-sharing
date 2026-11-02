"""Checkpoint helpers for jobs (plan §7.5).

Inside a job, write checkpoints with `pool.checkpoint.save(path)`. The agent
uploads the checkpoint directory every `checkpoint.every` and on shutdown, and
on retry mounts the newest one read-only so the job can resume with
`pool.checkpoint.latest()`. Jobs without checkpoints still work; they restart
from the beginning.

The directory is $POOL_CHECKPOINT_DIR (default /ckpt). Outside a job, point it
at any local directory to test resume logic.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

_PREFIX = "ckpt-"


def directory() -> Path:
    return Path(os.environ.get("POOL_CHECKPOINT_DIR", "/ckpt"))


def resume_directory() -> Path:
    """Where the agent mounts the newest checkpoint from a previous attempt (read-only)."""
    return Path(os.environ.get("POOL_RESUME_DIR", str(directory())))


def save(path: str | os.PathLike, name: str | None = None, keep: int = 3) -> Path:
    """Copy a file or directory into the checkpoint directory atomically.

    Returns the stored path. Older checkpoints beyond `keep` are removed.
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)
    dest_dir = directory()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"{_PREFIX}{time.time_ns():020d}-{name or src.name}"
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=dest_dir))
    try:
        staged = tmp / src.name
        if src.is_dir():
            shutil.copytree(src, staged)
        else:
            shutil.copy2(src, staged)
        os.replace(staged, final)  # atomic on the same filesystem
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _prune(dest_dir, keep)
    return final


def latest() -> Path | None:
    """Newest checkpoint available to resume from, or None."""
    for d in (resume_directory(), directory()):
        if d.is_dir():
            items = sorted(p for p in d.iterdir() if p.name.startswith(_PREFIX))
            if items:
                return items[-1]
    return None


def _prune(d: Path, keep: int) -> None:
    items = sorted(p for p in d.iterdir() if p.name.startswith(_PREFIX))
    for old in items[:-keep] if keep > 0 else []:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)
