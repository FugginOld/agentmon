"""Small per-run state shared between hook invocations (each hook is a fresh process).

Gemini CLI and Kimi CLI send no tool-call id on the "after" hook, so a step is paired with its start by
hashing the tool name and input; the oldest open step with that hash is closed first. Codex's per-request
token counts are accumulated here too, because ``usage`` events carry absolute totals.

Gemini and Kimi run tool calls in parallel and Codex runs async hooks concurrently, so every read-modify-write
holds a lock file. shortcut: if the lock can't be taken within ~2 s (or the OS refuses) the update goes ahead
unlocked rather than losing the event, so a pathological pile-up can still mis-pair a step.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from typing import Any

from .. import paths


def _file(run: str):
    return paths.home() / "state" / f"{paths.safe_id(run)}.json"


def _load(run: str) -> dict:
    try:
        state = json.loads(_file(run).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(run: str, state: dict) -> None:
    target = _file(run)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")  # per process: never shared between hooks
    tmp.write_text(json.dumps(state), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:  # Windows: the target is briefly held open by another process
            if attempt == 4:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.01)


@contextlib.contextmanager
def _locked(run: str):
    lock = _file(run).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline, held = time.monotonic() + 2, False
    while not held:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
        except (FileExistsError, PermissionError):  # busy (PermissionError: Windows, lock mid-delete)
            try:
                if time.time() - lock.stat().st_mtime > 5:  # left behind by a killed hook
                    stale = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
                    os.replace(lock, stale)  # one waiter wins the rename; it can't delete a lock someone just took
                    stale.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        except OSError:
            break
    try:
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                lock.unlink()


def _key(tool: str, tool_input: Any) -> str:
    text = tool + json.dumps(tool_input, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def fallback_id(tool: str, tool_input: Any) -> str:
    """Same id for a call's start and end when the state file can't be used (identical calls then share a step)."""
    return f"{_key(tool, tool_input)}-fb"


def begin(run: str, tool: str, tool_input: Any) -> str:
    """Id for a tool call that is starting."""
    key = _key(tool, tool_input)
    with _locked(run):
        state = _load(run)
        state["seq"] = state.get("seq", 0) + 1
        sid = f"{key}-{state['seq']}"
        state.setdefault("open", {}).setdefault(key, []).append(sid)
        _save(run, state)
    return sid


def end(run: str, tool: str, tool_input: Any) -> str:
    """Id of the start this finished call belongs to (a fresh id if its start was never seen)."""
    key = _key(tool, tool_input)
    with _locked(run):
        state = _load(run)
        queue = state.get("open", {}).get(key)
        if queue:
            sid = queue.pop(0)
            if not queue:
                del state["open"][key]
        else:
            state["seq"] = state.get("seq", 0) + 1
            sid = f"{key}-x{state['seq']}"
        _save(run, state)
    return sid


def add_tokens(run: str, agent: str, tokens: dict[str, int]) -> dict[str, int]:
    """Add one request's token counts to the agent's running totals and return the totals."""
    with _locked(run):
        state = _load(run)
        totals = state.setdefault("tokens", {}).setdefault(agent, {})
        for name, value in tokens.items():
            totals[name] = int(totals.get(name, 0)) + int(value or 0)
        _save(run, state)
    return dict(totals)


def clear(run: str) -> None:
    try:
        _file(run).unlink()
    except OSError:
        pass
