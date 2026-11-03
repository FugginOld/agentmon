"""Where agentmon keeps run logs.

Every run is one append-only JSONL file: ``$AGENTMON_HOME/runs/<run-id>.jsonl``.
``AGENTMON_HOME`` defaults to ``~/.agentmon``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def home() -> Path:
    override = os.environ.get("AGENTMON_HOME")
    return Path(override).expanduser() if override else Path.home() / ".agentmon"


def runs_dir() -> Path:
    return home() / "runs"


def safe_id(run_id: str) -> str:
    cleaned = _SAFE.sub("-", run_id.strip()).strip("-.")
    return cleaned[:120] or "run"


def run_file(run_id: str) -> Path:
    return runs_dir() / f"{safe_id(run_id)}.jsonl"
