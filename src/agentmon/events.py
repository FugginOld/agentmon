"""The agentmon event schema (v1).

One JSON object per line. Every event carries ``v``, ``ts``, ``run`` and ``type``;
the remaining fields depend on the type. See docs/EVENTS.md for the full spec.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
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
    """Raised when an event does not match the schema. ``code`` is a short stable reason (see REASONS)."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# Fixed texts for API clients: the exception message can echo the sender's own input, so it is not sent back.
REASONS = {
    "not_object": "an event must be a JSON object",
    "unknown_type": "unknown event type",
    "bad_run": "run must be a non-empty string",
    "missing_field": "a required field for this event type is missing",
    "bad_ts": "ts must be epoch seconds or an ISO-8601 string",
    "bad_status": "status is not valid for this event type",
    "bad_deps": "deps must be a list of step ids",
    "non_finite": "NaN and Infinity are not valid JSON numbers",
}


def _reject_constant(name: str) -> None:
    raise EventError(f"{name} is not a valid JSON number", "non_finite")


def loads(text: str | bytes) -> Any:
    """``json.loads`` that refuses the non-standard NaN / Infinity / -Infinity constants Python accepts."""
    return json.loads(text, parse_constant=_reject_constant)


def now_iso(t: float | None = None) -> str:
    when = _dt.datetime.fromtimestamp(time.time() if t is None else t, _dt.timezone.utc)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: Any) -> float:
    """Accept epoch seconds (int/float) or an ISO-8601 string; return epoch seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise EventError(f"bad ts: {value!r}", "non_finite")
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise EventError(f"bad ts: {value!r}", "bad_ts") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.timestamp()
    raise EventError(f"bad ts: {value!r}", "bad_ts")


def validate(event: Any) -> dict:
    if not isinstance(event, dict):
        raise EventError("event must be a JSON object", "not_object")
    etype = event.get("type")
    if etype not in TYPES:
        raise EventError(f"unknown event type: {etype!r}", "unknown_type")
    run = event.get("run")
    if not isinstance(run, str) or not run.strip():
        raise EventError("event.run must be a non-empty string", "bad_run")
    for name in REQUIRED.get(etype, ()):
        if event.get(name) in (None, ""):
            raise EventError(f"{etype} requires {name!r}", "missing_field")
    if "ts" in event:
        parse_ts(event["ts"])
    status = event.get("status")
    if etype in ("step_start", "step_end") and status is not None and status not in STEP_STATUSES:
        raise EventError(f"bad step status: {status!r}", "bad_status")
    deps = event.get("deps")
    if deps is not None and not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
        raise EventError("deps must be a list of step ids", "bad_deps")
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
        return validate(loads(line))
    except (ValueError, EventError):
        return None
