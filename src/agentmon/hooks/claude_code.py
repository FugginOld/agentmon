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
import shlex
import sys
import time
import traceback
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
def translate(payload: dict, now: float | None = None) -> list[dict]:
    """Map one Claude Code hook payload to zero or more agentmon events."""
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
        ev("run_start", title=title, cwd=cwd, source=f"claude-code:{payload.get('source', 'startup')}")
        ev("agent_start", agent=MAIN, name="Claude", model=payload.get("model"))
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


def handle_stdin(stream=None) -> int:
    """Entry point for ``agentmon hook``. Always returns 0 and never writes to stdout."""
    if os.environ.get("AGENTMON_DISABLE"):
        return 0
    try:
        raw = (stream or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        for event in translate(payload):
            sink.append(event)
    except Exception:  # noqa: BLE001 – a monitor must never break the agent
        try:
            log = paths.home() / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"--- {events.now_iso()}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


# --------------------------------------------------------------------------- install
def settings_path(scope: str, project_dir: Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    base = project_dir or Path.cwd()
    return base / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -m agentmon hook"


def _is_ours(entry: dict) -> bool:
    return any(MARKER in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def install(scope: str = "user", dry_run: bool = False, project_dir: Path | None = None) -> tuple[Path, dict]:
    path = settings_path(scope, project_dir)
    settings = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    hooks = settings.setdefault("hooks", {})
    command = hook_command()
    for event_name, is_async in HOOK_EVENTS.items():
        entries = [e for e in hooks.get(event_name, []) if not (isinstance(e, dict) and _is_ours(e))]
        hook = {"type": "command", "command": command, "timeout": 10}
        if is_async:
            hook["async"] = True
        entry: dict[str, Any] = {"hooks": [hook]}
        if event_name in TOOL_EVENTS:
            entry["matcher"] = "*"
        entries.append(entry)
        hooks[event_name] = entries
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".agentmon.bak")
            backup.write_text(path.read_text())
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return path, settings


def uninstall(scope: str = "user", dry_run: bool = False, project_dir: Path | None = None) -> tuple[Path, int]:
    path = settings_path(scope, project_dir)
    if not path.exists():
        return path, 0
    settings = json.loads(path.read_text() or "{}")
    hooks = settings.get("hooks", {})
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
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return path, removed
