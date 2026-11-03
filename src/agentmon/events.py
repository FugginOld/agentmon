"""The agentmon event schema (v1).

One JSON object per line. Every event carries ``v``, ``ts``, ``run`` and ``type``;
the remaining fields depend on the type. See docs/EVENTS.md for the full spec.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from typing import Any

SCHEMA_VERSION = 1

TYPES = frozenset(
    {
        "run_start",
        "run_end",
        "agent_start",
        "agent_end",
        "step_start",
        "step_end",
        "message",
        "thinking",
        "usage",
        "note",
    }
)

REQUIRED: dict[str, tuple[str, ...]] = {
    "agent_start": ("agent",),
    "agent_end": ("agent",),
    "step_start": ("step", "agent"),
    "step_end": ("step",),
    "message": ("agent", "text"),
    "thinking": ("agent", "text"),
    "usage": ("agent",),
    "note": ("text",),
}

STEP_STATUSES = frozenset({"queued", "running", "done", "error"})


class EventError(ValueError):
    """Raised when an event does not match the schema."""


def now_iso(t: float | None = None) -> str:
    when = _dt.datetime.fromtimestamp(time.time() if t is None else t, _dt.timezone.utc)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: Any) -> float:
    """Accept epoch seconds (int/float) or an ISO-8601 string; return epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise EventError(f"bad ts: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.timestamp()
    raise EventError(f"bad ts: {value!r}")


def validate(event: Any) -> dict:
    if not isinstance(event, dict):
        raise EventError("event must be a JSON object")
    etype = event.get("type")
    if etype not in TYPES:
        raise EventError(f"unknown event type: {etype!r}")
    run = event.get("run")
    if not isinstance(run, str) or not run.strip():
        raise EventError("event.run must be a non-empty string")
    for name in REQUIRED.get(etype, ()):
        if event.get(name) in (None, ""):
            raise EventError(f"{etype} requires {name!r}")
    if "ts" in event:
        parse_ts(event["ts"])
    status = event.get("status")
    if etype in ("step_start", "step_end") and status is not None and status not in STEP_STATUSES:
        raise EventError(f"bad step status: {status!r}")
    deps = event.get("deps")
    if deps is not None and not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
        raise EventError("deps must be a list of step ids")
    return event


def make(etype: str, run: str, ts: float | None = None, **fields: Any) -> dict:
    """Build and validate an event. ``None`` fields are dropped."""
    event = {"v": SCHEMA_VERSION, "ts": now_iso(ts), "run": run, "type": etype}
    event.update({k: v for k, v in fields.items() if v is not None})
    return validate(event)


def parse_line(line: str) -> dict | None:
    """Parse one JSONL line; return None for blank or invalid lines."""
    line = line.strip()
    if not line:
        return None
    try:
        return validate(json.loads(line))
    except (ValueError, EventError):
        return None
