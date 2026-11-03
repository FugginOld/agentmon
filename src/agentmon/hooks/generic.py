"""Hook adapters for Gemini CLI and Kimi CLI.

Neither sends a tool-call id on the "after" hook, so steps are paired through ``pairing``. Field names follow
each CLI's hooks reference; anything the docs don't pin down is read defensively.

Not mapped: Gemini ``AfterModel`` (fires per streamed chunk) and ``BeforeModel``, so no token counts for Gemini.
shortcut: Kimi sends no subagent id, only a name. Tool calls inside a subagent land on the main agent, and two
subagents with the same name share a lane (the first SubagentStop ends both). Upgrade if Kimi adds an id.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import events
from . import pairing
from .claude_code import MAIN, _clip_json, _short, _summary, step_title

FLAVORS: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "gemini-cli", "name": "Gemini", "prompt": "BeforeAgent", "pre": "BeforeTool", "post": "AfterTool",
        "fail": None, "response": "tool_response", "reply": "AfterAgent",
        "notes": {"Notification": "warn", "PreCompress": "info"},
    },
    "kimi": {
        "label": "kimi-cli", "name": "Kimi", "prompt": "UserPromptSubmit", "pre": "PreToolUse",
        "post": "PostToolUse", "fail": "PostToolUseFailure", "response": "tool_output", "reply": None,
        "notes": {"Notification": "warn", "PreCompact": "info", "PostCompact": "info", "StopFailure": "error"},
    },
}


def _first_line(value: Any, width: int = 48) -> str | None:
    if isinstance(value, str) and value.strip():
        return _short(value.strip().splitlines()[0], width)
    return None


def _result(tool: str, response: Any) -> tuple[str, str | None]:
    """(status, summary) for a finished tool call."""
    if isinstance(response, dict):
        err = response.get("error")
        if err:
            return "error", _short(err.get("message") if isinstance(err, dict) else err, 48)
        for key in ("returnDisplay", "llmContent"):
            line = _first_line(response.get(key))
            if line:
                return "done", line
    return "done", _summary(tool, response)


def _step_id(fn, run: str, tool: str, tool_input: Any) -> str:
    """Paired step id; if the state file is unusable, a deterministic id, so the event is still written and
    the call's start and end still meet (identical calls at that moment share one step)."""
    try:
        return fn(run, tool, tool_input)
    except Exception:  # noqa: BLE001
        return pairing.fallback_id(tool, tool_input)


def translate(payload: dict, flavor: str, now: float | None = None) -> list[dict]:
    """Map one Gemini CLI or Kimi CLI hook payload to agentmon events."""
    cfg = FLAVORS[flavor]
    name = payload.get("hook_event_name")
    run = payload.get("session_id")
    if not name or not run:
        return []
    ts = time.time() if now is None else now
    out: list[dict] = []

    def ev(etype: str, **fields: Any) -> None:
        out.append(events.make(etype, run, ts=ts, **fields))

    if name == "SessionStart":
        cwd = payload.get("cwd")
        ev("run_start", title=Path(cwd).name if cwd else None, cwd=cwd,
           source=f"{cfg['label']}:{payload.get('source', 'startup')}")
        ev("agent_start", agent=MAIN, name=cfg["name"], model=payload.get("model"))
    elif name == cfg["prompt"]:
        prompt = payload.get("prompt") or ""
        sid = f"prompt-{int(ts * 1000)}"
        ev("message", agent="user", to=MAIN, text=_short(prompt, 200) or "(prompt)")
        ev("step_start", step=sid, agent=MAIN, title=f"Prompt · {_short(prompt, 28)}", input=_clip_json(prompt))
        ev("step_end", step=sid, status="done", summary="received")
    elif name == cfg["pre"]:
        tool = payload.get("tool_name") or "tool"
        tool_input = payload.get("tool_input")
        sid = _step_id(pairing.begin, run, tool, tool_input)
        ev("step_start", step=sid, agent=MAIN, tool=tool, title=step_title(tool, tool_input),
           input=_clip_json(tool_input))
    elif name in (cfg["post"], cfg["fail"]):
        tool = payload.get("tool_name") or "tool"
        sid = _step_id(pairing.end, run, tool, payload.get("tool_input"))
        if name == cfg["fail"]:
            err = payload.get("error") or payload.get("message")
            status, summary, output = "error", _short(err, 48) if err else "failed", _clip_json(err)
        else:
            response = payload.get(cfg["response"])
            status, summary = _result(tool, response)
            output = _clip_json(response) if response is not None else None
        ev("step_end", step=sid, agent=MAIN, tool=tool, status=status, summary=summary, output=output)
    elif name == cfg["reply"]:
        text = payload.get("prompt_response") or payload.get("response") or payload.get("last_assistant_message")
        if text:
            ev("message", agent=MAIN, to="user", text=_short(text, 200))
    elif name in cfg["notes"]:
        text = (payload.get("message") or payload.get("body") or payload.get("title") or payload.get("error_message")
                or (f"compacting context ({payload['trigger']})" if payload.get("trigger") else name))
        ev("note", agent=MAIN, text=_short(text, 200), level=cfg["notes"][name])
    elif name == "SubagentStart" and payload.get("agent_name"):
        ev("agent_start", agent=payload["agent_name"], name=payload["agent_name"], kind=payload["agent_name"],
           parent=MAIN)
    elif name == "SubagentStop" and payload.get("agent_name"):
        ev("agent_end", agent=payload["agent_name"], status="done", summary=_first_line(payload.get("response"), 60))
    elif name == "SessionEnd":
        ev("run_end", status="done")
        pairing.clear(run)
    return out
