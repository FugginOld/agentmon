"""Reduce an event stream into the state of one run.

``Run.apply(event)`` is the only way state changes. Both UIs read ``Run.snapshot()``
(plain JSON-able dicts) so the web and terminal front ends always agree.

The reducer is deliberately forgiving: hooks can fire asynchronously, so a
``step_end`` may arrive before its ``step_start``, and producers may omit
optional fields. Nothing here raises on odd-but-valid input.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .events import parse_ts

PALETTE = ["#B49CFF", "#6EA8FE", "#F08BCB", "#5FD4C4", "#C9D36A", "#F5A86E", "#8FD3FF", "#E58B8B"]
USER = "user"
TOOLS = "tools"
SPAWN_TOOLS = frozenset({"Task", "Agent"})
STALE_AFTER = 600.0  # seconds without events before an open run is treated as stale
LOG_LIMIT = 500
TEXT_LIMIT = 4000


def _clip(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Agent:
    id: str
    name: str
    color: str
    model: str | None = None
    parent: str | None = None
    spawned_by: str | None = None
    kind: str | None = None
    status: str = "running"
    started: float | None = None
    ended: float | None = None
    tokens: dict = field(default_factory=dict)
    has_usage: bool = False
    context: dict | None = None
    cost: float | None = None
    last_thought: str | None = None


@dataclass
class Step:
    id: str
    agent: str
    title: str
    seq: int
    tool: str | None = None
    input: Any = None
    deps: list[str] = field(default_factory=list)
    status: str = "running"
    created: float = 0.0
    started: float | None = None
    ended: float | None = None
    summary: str | None = None
    output: Any = None
    tokens: dict = field(default_factory=dict)
    explicit_deps: bool = False


@dataclass
class Message:
    ts: float
    src: str
    dst: str
    label: str
    kind: str  # call | return | spawn | message | error
    step: str | None = None


class Run:
    def __init__(self, run_id: str):
        self.id = run_id
        self.title: str | None = None
        self.cwd: str | None = None
        self.source: str | None = None
        self.status = "running"
        self.started: float | None = None
        self.ended: float | None = None
        self.last_ts: float = 0.0
        self.agents: dict[str, Agent] = {}
        self.steps: dict[str, Step] = {}
        self.order: list[str] = []
        self.messages: list[Message] = []
        self.log: list[dict] = []
        self.errors = 0
        self.event_count = 0

    # ------------------------------------------------------------------ helpers
    def clock(self) -> float:
        """'Now' for this run: the end time if finished, else wall time (frozen once stale)."""
        if self.ended is not None and self.status != "running":
            return self.ended
        now = time.time()
        if self.last_ts and now - self.last_ts > STALE_AFTER:
            return self.last_ts
        return max(now, self.last_ts or now)

    @property
    def stale(self) -> bool:
        return self.status == "running" and bool(self.last_ts) and time.time() - self.last_ts > STALE_AFTER

    def agent(self, agent_id: str, ts: float | None = None) -> Agent:
        found = self.agents.get(agent_id)
        if found is None:
            color = PALETTE[len(self.agents) % len(PALETTE)]
            found = Agent(id=agent_id, name=agent_id, color=color, started=ts)
            self.agents[agent_id] = found
        return found

    def _log(self, ts: float, etype: str, agent: str | None, text: str, level: str = "info") -> None:
        self.log.append({"ts": ts, "type": etype, "agent": agent, "text": text, "level": level})
        if len(self.log) > LOG_LIMIT:
            del self.log[: len(self.log) - LOG_LIMIT]

    def _infer_deps(self, step: Step) -> list[str]:
        for sid in reversed(self.order):
            if sid != step.id and self.steps[sid].agent == step.agent:
                return [sid]
        owner = self.agents.get(step.agent)
        if owner and owner.spawned_by and owner.spawned_by in self.steps:
            return [owner.spawned_by]
        return []

    def _open_spawn_step(self, parent: str, kind: str | None) -> str | None:
        linked = {a.spawned_by for a in self.agents.values() if a.spawned_by}
        fallback = None
        for sid in reversed(self.order):
            step = self.steps[sid]
            if step.agent != parent or step.tool not in SPAWN_TOOLS or sid in linked:
                continue
            wanted = step.input.get("subagent_type") if isinstance(step.input, dict) else None
            if kind and wanted == kind:
                return sid
            fallback = fallback or sid
        return fallback

    # ------------------------------------------------------------------ reducer
    def apply(self, ev: dict) -> None:
        ts = parse_ts(ev["ts"]) if "ts" in ev else time.time()
        self.event_count += 1
        self.last_ts = max(self.last_ts, ts)
        if self.started is None or ts < self.started:
            self.started = ts
        handler = getattr(self, "_on_" + ev["type"], None)
        if handler:
            handler(ev, ts)

    def _on_run_start(self, ev: dict, ts: float) -> None:
        self.title = ev.get("title") or self.title
        self.cwd = ev.get("cwd") or self.cwd
        self.source = ev.get("source") or self.source
        self.status = "running"
        self.ended = None
        self._log(ts, "run_start", None, f"run started{': ' + self.title if self.title else ''}")

    def _on_run_end(self, ev: dict, ts: float) -> None:
        self.status = ev.get("status") or "done"
        self.ended = ts
        self._log(ts, "run_end", None, f"run {self.status}", "error" if self.status == "error" else "info")

    def _on_agent_start(self, ev: dict, ts: float) -> None:
        a = self.agent(ev["agent"], ts)
        a.name = ev.get("name") or a.name
        a.model = ev.get("model") or a.model
        a.kind = ev.get("kind") or a.kind
        a.status = "running"
        a.started = a.started if a.started is not None else ts
        if ev.get("parent") and ev["parent"] != a.id:
            a.parent = ev["parent"]
            self.agent(a.parent, ts)
        a.spawned_by = ev.get("spawned_by") or a.spawned_by
        if a.parent and not a.spawned_by:
            a.spawned_by = self._open_spawn_step(a.parent, a.kind or ev.get("name"))
        if a.parent:
            self.messages.append(Message(ts, a.parent, a.id, f"spawn {a.name}", "spawn", a.spawned_by))
        self._log(ts, "agent_start", a.id, f"{a.name} started" + (f" ({a.model})" if a.model else ""))

    def _on_agent_end(self, ev: dict, ts: float) -> None:
        a = self.agent(ev["agent"], ts)
        a.status = ev.get("status") or "done"
        a.ended = ts
        summary = ev.get("summary")
        if a.parent:
            kind = "error" if a.status == "error" else "return"
            self.messages.append(Message(ts, a.id, a.parent, _clip(summary, 60) or a.status, kind))
        self._log(ts, "agent_end", a.id, f"{a.name} {a.status}" + (f": {summary}" if summary else ""),
                  "error" if a.status == "error" else "info")

    def _on_step_start(self, ev: dict, ts: float) -> None:
        sid = ev["step"]
        agent_id = ev["agent"]
        self.agent(agent_id, ts)
        status = ev.get("status") or "running"
        step = self.steps.get(sid)
        if step is None:
            step = Step(id=sid, agent=agent_id, title=ev.get("title") or ev.get("tool") or sid,
                        seq=len(self.order), created=ts)
            self.steps[sid] = step
            self.order.append(sid)
            fresh = True
        else:
            fresh = False
            step.agent = agent_id or step.agent
            if ev.get("title"):
                step.title = ev["title"]
        step.tool = ev.get("tool") or step.tool
        if "input" in ev:
            step.input = ev["input"]
        if "deps" in ev:
            step.deps = [d for d in ev["deps"] if d != sid]
            step.explicit_deps = True
        elif fresh:
            step.deps = self._infer_deps(step)
        promoted = False
        if step.ended is None:  # never downgrade a step that already finished
            if status == "running":
                if step.started is None:
                    step.started = ts
                    promoted = True
                step.status = "running"
            elif status == "queued" and step.started is None:
                step.status = "queued"
        if promoted and step.tool:
            self.messages.append(Message(ts, agent_id, TOOLS, step.title, "call", sid))
        verb = "queued" if status == "queued" else "start"
        self._log(ts, "step_start", agent_id, f"{verb} {step.title}" + (f" [{step.tool}]" if step.tool else ""))

    def _on_step_end(self, ev: dict, ts: float) -> None:
        sid = ev["step"]
        step = self.steps.get(sid)
        if step is None:  # end arrived first (async hooks) – synthesise the step
            agent_id = ev.get("agent") or "main"
            self.agent(agent_id, ts)
            step = Step(id=sid, agent=agent_id, title=ev.get("title") or ev.get("tool") or sid,
                        seq=len(self.order), created=ts, tool=ev.get("tool"))
            step.deps = self._infer_deps(step)
            self.steps[sid] = step
            self.order.append(sid)
        step.status = ev.get("status") or "done"
        step.ended = ts
        if step.started is None:
            step.started = ts
        step.summary = ev.get("summary") or step.summary
        if "output" in ev:
            step.output = ev["output"]
        if isinstance(ev.get("tokens"), dict):
            step.tokens = ev["tokens"]
        if step.status == "error":
            self.errors += 1
        if step.tool:
            kind = "error" if step.status == "error" else "return"
            label = step.summary or ("error" if step.status == "error" else "ok")
            self.messages.append(Message(ts, TOOLS, step.agent, _clip(label, 60) or "", kind, sid))
        self._log(ts, "step_end", step.agent,
                  f"{'failed' if step.status == 'error' else 'done'} {step.title}" + (f": {step.summary}" if step.summary else ""),
                  "error" if step.status == "error" else "info")

    def _on_message(self, ev: dict, ts: float) -> None:
        src = ev["agent"]
        dst = ev.get("to") or USER
        for who in (src, dst):
            if who not in (USER, TOOLS):
                self.agent(who, ts)
        kind = ev.get("kind") if ev.get("kind") in ("message", "error") else "message"
        self.messages.append(Message(ts, src, dst, _clip(ev["text"], 60) or "", kind))
        self._log(ts, "message", src, f"→ {dst}: {ev['text']}", "error" if kind == "error" else "info")

    def _on_thinking(self, ev: dict, ts: float) -> None:
        a = self.agent(ev["agent"], ts)
        a.last_thought = _clip(ev["text"], 1200)
        self._log(ts, "thinking", a.id, ev["text"])

    def _on_usage(self, ev: dict, ts: float) -> None:
        a = self.agent(ev["agent"], ts)
        if isinstance(ev.get("tokens"), dict):
            a.tokens = {k: int(v) for k, v in ev["tokens"].items() if isinstance(v, (int, float))}
            a.has_usage = True
        if isinstance(ev.get("context"), dict):
            a.context = ev["context"]
        if isinstance(ev.get("cost"), (int, float)):
            a.cost = float(ev["cost"])
        a.model = ev.get("model") or a.model

    def _on_note(self, ev: dict, ts: float) -> None:
        level = ev.get("level") or "info"
        if level == "error":
            self.errors += 1
        self._log(ts, "note", ev.get("agent"), ev["text"], level)

    # ------------------------------------------------------------------ queries
    def agent_tokens(self, agent_id: str) -> dict:
        a = self.agents[agent_id]
        if a.has_usage:
            return dict(a.tokens)
        total: dict[str, int] = {}
        for step in self.steps.values():
            if step.agent == agent_id:
                for k, v in step.tokens.items():
                    if isinstance(v, (int, float)):
                        total[k] = total.get(k, 0) + int(v)
        return total

    def metrics(self) -> dict:
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        tools: dict[str, int] = {}
        for step in self.steps.values():
            counts[step.status] = counts.get(step.status, 0) + 1
            if step.tool and step.started is not None:
                tools[step.tool] = tools.get(step.tool, 0) + 1
        tokens = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        for aid in self.agents:
            for k, v in self.agent_tokens(aid).items():
                tokens[k] = tokens.get(k, 0) + v
        costs = [a.cost for a in self.agents.values() if a.cost is not None]
        total_in = tokens["in"] + tokens["cache_read"] + tokens.get("cache_write", 0)
        clock = self.clock()
        return {
            "steps_total": len(self.steps),
            "steps": counts,
            "tool_calls": sum(tools.values()),
            "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
            "tokens": tokens,
            "cache_hit": (tokens["cache_read"] / total_in) if total_in else None,
            "cost": sum(costs) if costs else None,
            "errors": self.errors,
            "elapsed": (clock - self.started) if self.started else 0.0,
            "agents_total": len(self.agents),
            "agents_running": sum(1 for a in self.agents.values() if a.status == "running"),
        }

    def focus_step(self) -> str | None:
        running = [s for s in self.order if self.steps[s].status == "running"]
        if running:
            return max(running, key=lambda s: self.steps[s].started or 0)
        return self.order[-1] if self.order else None

    def snapshot(self) -> dict:
        from . import layout  # local import keeps the hook path light

        clock = self.clock()
        status = "stale" if self.stale else self.status
        return {
            "run": {"id": self.id, "title": self.title, "cwd": self.cwd, "source": self.source, "status": status,
                    "started": self.started, "ended": self.ended, "now": clock, "events": self.event_count},
            "agents": [
                {"id": a.id, "name": a.name, "model": a.model, "kind": a.kind, "parent": a.parent,
                 "spawned_by": a.spawned_by, "status": a.status, "color": a.color, "started": a.started,
                 "ended": a.ended, "tokens": self.agent_tokens(a.id), "context": a.context, "cost": a.cost,
                 "last_thought": a.last_thought}
                for a in self.agents.values()
            ],
            "steps": [self.step_dict(self.steps[sid], clock) for sid in self.order],
            "metrics": self.metrics(),
            "log": self.log[-200:],
            "focus": self.focus_step(),
            "layouts": {
                "swim": layout.swimlanes(self),
                "dag": layout.dag(self),
                "seq": layout.sequence(self),
                "timeline": layout.timeline(self),
            },
        }

    def step_dict(self, s: Step, clock: float | None = None) -> dict:
        clock = clock if clock is not None else self.clock()
        end = s.ended if s.ended is not None else (clock if s.started is not None else None)
        return {"id": s.id, "agent": s.agent, "title": s.title, "tool": s.tool, "input": _clip(s.input),
                "deps": list(s.deps), "status": s.status, "started": s.started, "ended": s.ended,
                "duration": (end - s.started) if (end is not None and s.started is not None) else None,
                "summary": s.summary, "output": _clip(s.output), "tokens": s.tokens, "seq": s.seq}


def load(events) -> Run:
    """Build a Run from an iterable of events (first event's run id names it)."""
    run: Run | None = None
    for ev in events:
        if run is None:
            run = Run(ev["run"])
        run.apply(ev)
    return run or Run("empty")
