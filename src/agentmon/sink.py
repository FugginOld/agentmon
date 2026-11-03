"""Append events to a run's JSONL file (safe for concurrent writers)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths
from .events import EventError, validate

try:  # POSIX advisory locking; Windows falls back to O_APPEND semantics only.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def append(event: dict, path: Path | None = None) -> Path:
    validate(event)
    target = path or paths.run_file(event["run"])
    # Run logs hold prompts, commands and tool output: new folders and files are owner-only (existing ones are left alone).
    if target.parent == paths.runs_dir():
        paths.home().mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False) + "\n"
    except ValueError as exc:
        raise EventError("event contains NaN or Infinity", "non_finite") from exc
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
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
