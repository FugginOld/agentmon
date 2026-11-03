"""Claude Code hook adapter.

``agentmon hook`` is registered as a command hook for the events in ``HOOK_EVENTS``.
Claude Code passes a JSON payload on stdin; we translate it into agentmon events,
append them to ``~/.agentmon/runs/<session_id>.jsonl`` and exit 0 with no stdout,
which Claude Code treats as a no-op (it never blocks or alters a tool call).

Field names follow the Claude Code hooks reference. Payload fields that the docs
don't pin down (the tool result, the failure text) are read defensively.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import events, paths, sink

MAIN = "main"

# event -> registered asynchronously? (async hooks never delay Claude Code)
HOOK_EVENTS: dict[str, bool] = {
    "SessionStart": False,
    "UserPromptSubmit": True,
    "PreToolUse": True,
    "PostToolUse": True,
    "PostToolUseFailure": True,
    "SubagentStart": True,
    "SubagentStop": True,
    "Notification": True,
    "PreCompact": True,
    "Stop": True,
    "SessionEnd": False,
}
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
MARKER = "agentmon hook"
INPUT_LIMIT = 4000


# --------------------------------------------------------------------------- titles
def _short(text: Any, width: int = 40) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def step_title(tool: str, tool_input: Any) -> str:
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"{parts[-1]} ({parts[1]})" if len(parts) >= 3 else tool
    if tool == "Bash":
        return f"Bash · {_short(ti.get('description') or ti.get('command'), 34)}"
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = ti.get("file_path") or ti.get("notebook_path") or ""
        return f"{tool} · {Path(str(target)).name or '?'}"
    if tool in ("Grep", "Glob"):
        return f"{tool} · {_short(ti.get('pattern'), 30)}"
    if tool in ("Task", "Agent"):
        who = ti.get("subagent_type") or ti.get("description") or "subagent"
        return f"Spawn {_short(who, 30)}"
    if tool == "WebFetch":
        url = str(ti.get("url") or "")
        return f"WebFetch · {_short(url.split('//')[-1].split('/')[0], 30)}"
    if tool == "WebSearch":
        return f"WebSearch · {_short(ti.get('query'), 30)}"
    if tool in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return f"{tool}"
    if tool == "apply_patch":  # Codex: the patch text is in tool_input.command
        m = re.search(r"\*\*\* (?:Update|Add|Delete) File: (.+)", str(ti.get("command") or ""))
        return f"apply_patch · {Path(m.group(1).strip()).name}" if m else tool
    # Other CLIs name their tools differently (run_shell_command, ReadFile, ...): show the first useful argument.
    for key in ("file_path", "absolute_path", "path"):
        if ti.get(key):
            return f"{tool} · {Path(str(ti[key])).name or '?'}"
    for key in ("description", "command", "pattern", "query", "url"):
        if ti.get(key):
            return f"{tool} · {_short(ti[key], 30)}"
    return tool


def _summary(tool: str, response: Any) -> str | None:
    if response is None:
        return None
    if isinstance(response, str):
        first = response.strip().splitlines()[0] if response.strip() else ""
        return _short(first, 48) or "ok"
    if isinstance(response, dict):
        for key in ("exitCode", "exit_code", "returncode"):
            if isinstance(response.get(key), int):
                return f"exit {response[key]}"
        if response.get("interrupted"):
            return "interrupted"
        for key in ("stdout", "output", "result", "content"):
            val = response.get(key)
            if isinstance(val, str) and val.strip():
                return _short(val.strip().splitlines()[0], 48)
        if "numFiles" in response:
            return f"{response['numFiles']} files"
        if response.get("filePath") or response.get("file_path"):
            return "ok"
    if isinstance(response, list):
        return f"{len(response)} items"
    return "ok"


def _clip_json(value: Any, limit: int = INPUT_LIMIT) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(text) <= limit:
        return value
    return text[: limit - 1] + "…"


# --------------------------------------------------------------------------- usage
def transcript_usage(path: str | None) -> dict | None:
    """Best-effort token totals from a Claude Code transcript JSONL (deduped by message id)."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    seen: dict[str, dict] = {}
    model = None
    last_ctx = None
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                usage = msg["usage"]
                seen[msg.get("id") or f"line{i}"] = usage
                model = msg.get("model") or model
                last_ctx = (usage.get("input_tokens", 0) or 0) + (usage.get("cache_read_input_tokens", 0) or 0) + (
                    usage.get("cache_creation_input_tokens", 0) or 0)
    except OSError:
        return None
    if not seen:
        return None
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    for u in seen.values():
        totals["in"] += int(u.get("input_tokens", 0) or 0)
        totals["out"] += int(u.get("output_tokens", 0) or 0)
        totals["cache_read"] += int(u.get("cache_read_input_tokens", 0) or 0)
        totals["cache_write"] += int(u.get("cache_creation_input_tokens", 0) or 0)
    out: dict[str, Any] = {"tokens": totals}
    if model:
        out["model"] = model
    if last_ctx:
        out["context"] = {"used": last_ctx}
    return out


# --------------------------------------------------------------------------- mapping
def translate(payload: dict, now: float | None = None, *, label: str = "claude-code",
              agent_name: str = "Claude") -> list[dict]:
    """Map one Claude Code hook payload to zero or more agentmon events.

    Codex and Qwen Code send the same event names and fields, so they reuse this with their own label and name.
    """
    name = payload.get("hook_event_name")
    run = payload.get("session_id")
    if not name or not run:
        return []
    ts = time.time() if now is None else now
    agent = payload.get("agent_id") or MAIN
    out: list[dict] = []

    def ev(etype: str, **fields: Any) -> None:
        out.append(events.make(etype, run, ts=ts, **fields))

    if name == "SessionStart":
        cwd = payload.get("cwd")
        title = payload.get("session_title") or (Path(cwd).name if cwd else None)
        ev("run_start", title=title, cwd=cwd, source=f"{label}:{payload.get('source', 'startup')}")
        ev("agent_start", agent=MAIN, name=agent_name, model=payload.get("model"))
    elif name == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        sid = f"prompt-{payload.get('prompt_id') or int(ts * 1000)}"
        ev("message", agent="user", to=MAIN, text=_short(prompt, 200) or "(prompt)")
        ev("step_start", step=sid, agent=MAIN, title=f"Prompt · {_short(prompt, 28)}", input=_clip_json(prompt))
        ev("step_end", step=sid, status="done", summary="received")
    elif name in TOOL_EVENTS:
        tool = payload.get("tool_name") or "tool"
        sid = payload.get("tool_use_id") or f"{tool}-{int(ts * 1000)}"
        if name == "PreToolUse":
            ev("step_start", step=sid, agent=agent, tool=tool, title=step_title(tool, payload.get("tool_input")),
               input=_clip_json(payload.get("tool_input")))
        elif name == "PostToolUse":
            response = payload.get("tool_response", payload.get("tool_result"))
            ev("step_end", step=sid, agent=agent, tool=tool, status="done", summary=_summary(tool, response),
               output=_clip_json(response) if response is not None else None)
        else:
            err = payload.get("error") or payload.get("error_message") or payload.get("message")
            ev("step_end", step=sid, agent=agent, tool=tool, status="error",
               summary=_short(err, 48) if err else ("interrupted" if payload.get("is_interrupt") else "failed"),
               output=_clip_json(err) if err else None)
    elif name == "SubagentStart":
        kind = payload.get("agent_type") or "subagent"
        if payload.get("agent_id"):
            ev("agent_start", agent=payload["agent_id"], name=kind, kind=kind, parent=MAIN)
    elif name == "SubagentStop":
        if payload.get("agent_id"):
            usage = transcript_usage(payload.get("agent_transcript_path"))
            if usage:
                ev("usage", agent=payload["agent_id"], **usage)
            ev("agent_end", agent=payload["agent_id"], status="done",
               summary=_short(payload.get("last_assistant_message"), 60) or None)
    elif name == "Stop":
        usage = transcript_usage(payload.get("transcript_path"))
        if usage:
            ev("usage", agent=MAIN, **usage)
        if payload.get("last_assistant_message"):
            ev("message", agent=MAIN, to="user", text=_short(payload["last_assistant_message"], 200))
    elif name == "Notification":
        ev("note", agent=agent, text=_short(payload.get("message") or payload.get("title") or "notification", 200),
           level="warn")
    elif name == "PreCompact":
        ev("note", agent=agent, text=f"compacting context ({payload.get('trigger', 'auto')})")
    elif name == "SessionEnd":
        ev("run_end", status="done")
    return out


def run_hook(translate_fn: Callable[[dict], list[dict]], stream=None, json_stdout: bool = False) -> int:
    """Read one hook payload, append its events, return 0. Never raises.

    stdout stays empty, except ``{}`` for CLIs that insist on JSON from every hook.
    """
    try:
        if not os.environ.get("AGENTMON_DISABLE"):
            raw = (stream or sys.stdin).read()
            payload = json.loads(raw) if raw.strip() else {}
            for event in translate_fn(payload):
                sink.append(event)
    except Exception:  # noqa: BLE001 – a monitor must never break the agent
        try:
            log = paths.home() / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"--- {events.now_iso()}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    if json_stdout:
        try:
            sys.stdout.write("{}")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 – closed pipe, no stdout...
            try:  # keep the interpreter's exit flush from failing too
                sys.stdout = open(os.devnull, "w")  # noqa: SIM115
            except OSError:
                pass
    return 0


def handle_stdin(stream=None) -> int:
    """Entry point for ``agentmon hook``. Always returns 0 and never writes to stdout."""
    return run_hook(translate, stream)


# --------------------------------------------------------------------------- install
@dataclass(frozen=True)
class Target:
    """Where one CLI keeps its hook settings, and how it wants hooks registered."""

    events: dict[str, bool]  # event -> registered asynchronously?
    agent: str = "claude"
    dirname: str = ".claude"
    filename: str = "settings.json"
    local_filename: str | None = "settings.local.json"
    tool_events: frozenset = TOOL_EVENTS
    matcher: str | None = "*"  # None: leave the matcher out so the hook runs for every tool
    timeout: int = 10  # the unit is the CLI's own (Gemini: milliseconds)
    timeouts: dict[str, int] = field(default_factory=dict)  # per-event overrides


CLAUDE = Target(events=HOOK_EVENTS)


def read_text_exact(path: Path) -> str:
    """UTF-8 and the file's own line endings (the locale codec and universal newlines would corrupt both)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text_exact(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_backup(path: Path) -> None:
    """Copy ``path`` to ``<path>.agentmon.bak``, owner-only: settings files can hold API keys."""
    backup = path.with_suffix(path.suffix + ".agentmon.bak")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(path.read_bytes())
    try:
        os.chmod(backup, 0o600)  # a backup left by an earlier run keeps its old mode otherwise
    except OSError:
        pass


def _refuse_links(path: Path) -> None:
    """Project and local settings live in a repo that may not be yours: never write through its symlinks."""
    for p in (path, path.parent, path.with_suffix(path.suffix + ".agentmon.bak")):
        if p.is_symlink():
            raise ValueError(f"{p} is a symlink; agentmon won't write through it in a project folder")


def _load_settings(path: Path) -> tuple[dict, str, bool]:
    """(settings, newline, had_bom) of a JSON settings file; ({}, LF, False) when missing or empty."""
    text = read_text_exact(path) if path.exists() else ""
    bom = text.startswith("﻿")
    text = text[1:] if bom else text
    nl = "\r\n" if "\r\n" in text else "\n"
    if not text.strip():
        return {}, nl, bom
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not plain JSON ({exc}); agentmon won't touch it. Fix it or remove any comments first.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data, nl, bom


def _check_hooks(hooks: Any, path: Path) -> None:
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        raise ValueError(f"{path}: 'hooks' must be an object mapping event names to lists; agentmon won't touch it")


def _dump_settings(settings: dict, nl: str) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False).replace("\n", nl) + nl


def settings_path(scope: str, project_dir: Path | None = None, target: Target = CLAUDE) -> Path:
    if scope == "user":
        return Path.home() / target.dirname / target.filename
    base = project_dir or Path.cwd()
    return base / target.dirname / (target.local_filename if scope == "local" and target.local_filename else target.filename)


def hook_command(agent: str = "claude") -> str:
    suffix = "" if agent == "claude" else f" --agent {agent}"
    # -P (3.11+) / -I (3.10) keep the working directory, i.e. the project being edited, off sys.path:
    # otherwise an `agentmon/` folder inside a cloned repo would run instead of this package.
    guard = "-P" if sys.version_info >= (3, 11) else "-I"
    return f"{shlex.quote(sys.executable)} {guard} -m agentmon hook{suffix}"


def _is_ours(entry: dict) -> bool:
    return any(MARKER in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def install(scope: str = "user", dry_run: bool = False, project_dir: Path | None = None,
            target: Target = CLAUDE) -> tuple[Path, dict]:
    path = settings_path(scope, project_dir, target)
    settings, nl, bom = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    _check_hooks(hooks, path)
    command = hook_command(target.agent)
    for event_name, is_async in target.events.items():
        entries = [e for e in hooks.get(event_name, []) if not (isinstance(e, dict) and _is_ours(e))]
        hook = {"type": "command", "command": command, "timeout": target.timeouts.get(event_name, target.timeout)}
        if is_async:
            hook["async"] = True
        entry: dict[str, Any] = {"hooks": [hook]}
        if target.matcher and event_name in target.tool_events:
            entry["matcher"] = target.matcher
        entries.append(entry)
        hooks[event_name] = entries
    if not dry_run:
        if scope != "user":
            _refuse_links(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            write_backup(path)
        write_text_exact(path, ("﻿" if bom else "") + _dump_settings(settings, nl))
    return path, settings


def uninstall(scope: str = "user", dry_run: bool = False, project_dir: Path | None = None,
              target: Target = CLAUDE) -> tuple[Path, int]:
    path = settings_path(scope, project_dir, target)
    if not path.exists():
        return path, 0
    settings, nl, bom = _load_settings(path)
    hooks = settings.get("hooks", {})
    _check_hooks(hooks, path)
    removed = 0
    for event_name in list(hooks):
        kept = [e for e in hooks[event_name] if not (isinstance(e, dict) and _is_ours(e))]
        removed += len(hooks[event_name]) - len(kept)
        if kept:
            hooks[event_name] = kept
        else:
            del hooks[event_name]
    if not hooks:
        settings.pop("hooks", None)
    if not dry_run and removed:
        if scope != "user":
            _refuse_links(path)
        write_text_exact(path, ("﻿" if bom else "") + _dump_settings(settings, nl))
    return path, removed
