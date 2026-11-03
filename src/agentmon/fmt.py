"""Small formatting helpers shared by the CLI and both UIs."""

from __future__ import annotations

import datetime as _dt


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def clock(seconds: float | None) -> str:
    """Elapsed time as MM:SS (or H:MM:SS)."""
    if seconds is None:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def tokens(n: int | float | None) -> str:
    if not n:
        return "0"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{int(n)}"


def money(x: float | None) -> str:
    return "—" if x is None else f"${x:.2f}"


def hms(ts: float | None) -> str:
    if ts is None:
        return "--:--:--"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def truncate(text: str | None, width: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"
