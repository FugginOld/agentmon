"""Append events to a run's JSONL file (safe for concurrent writers)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths
from .events import validate

try:  # POSIX advisory locking; Windows falls back to O_APPEND semantics only.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def append(event: dict, path: Path | None = None) -> Path:
    validate(event)
    target = path or paths.run_file(event["run"])
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return target


def append_many(events: list[dict], path: Path | None = None) -> None:
    for event in events:
        append(event, path)
