"""A scripted multi-agent run for trying the UIs without a real agent.

``agentmon demo`` replays it in real time (or faster); ``--instant --until 55``
writes a frozen snapshot that matches the design mockups.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import events, sink

# (seconds from start, type, fields)
SCRIPT: list[tuple[float, str, dict]] = [
    (0.0, "run_start", {"title": "auth-pkce-refactor", "cwd": "~/repos/webapp", "source": "demo"}),
    (0.0, "agent_start", {"agent": "orch", "name": "Orchestrator", "model": "opus"}),
    (0.0, "message", {"agent": "user", "to": "orch", "text": "Refactor the auth module to use OAuth2 PKCE"}),
    (0.0, "step_start", {"step": "s1", "agent": "orch", "title": "Prompt received"}),
    (0.5, "step_end", {"step": "s1", "summary": "user · 1.2k tok", "tokens": {"in": 1200}}),
    (0.5, "step_start", {"step": "s2", "agent": "orch", "title": "Plan · 6 steps"}),
    (8.6, "step_start", {"step": "s3", "agent": "orch", "tool": "Agent", "title": "Spawn explorer",
                         "input": {"subagent_type": "explorer", "description": "map the login flow"}}),
    (8.7, "step_end", {"step": "s2", "summary": "6 steps", "tokens": {"in": 3100, "out": 820}}),
    (8.7, "agent_start", {"agent": "explorer", "name": "Explorer", "model": "haiku", "parent": "orch",
                          "kind": "explorer", "spawned_by": "s3"}),
    (9.0, "step_start", {"step": "e1", "agent": "explorer", "tool": "Grep", "title": "Grep · login flow",
                         "input": {"pattern": "authenticate\\(", "path": "auth/"}}),
    (9.4, "step_end", {"step": "e1", "summary": "14 hits"}),
    (9.4, "step_start", {"step": "e2", "agent": "explorer", "tool": "Read", "title": "Read · 9 files",
                         "input": {"files": 9}}),
    (11.5, "step_end", {"step": "e2", "summary": "18.6k tok", "tokens": {"in": 18600}}),
    (11.5, "step_start", {"step": "e3", "agent": "explorer", "title": "Summarize findings"}),
    (12.9, "step_end", {"step": "e3", "summary": "1.9k out", "tokens": {"out": 1900}}),
    (12.9, "agent_end", {"agent": "explorer", "status": "done", "summary": "findings summary"}),
    (12.9, "step_end", {"step": "s3", "summary": "explorer returned findings"}),
    (13.0, "step_start", {"step": "s4", "agent": "orch", "title": "Synthesize context", "deps": ["s3", "e3"]}),
    (17.4, "step_end", {"step": "s4", "summary": "context ready", "tokens": {"in": 2700, "out": 600}}),
    (17.4, "step_start", {"step": "s5", "agent": "orch", "tool": "Agent", "title": "Spawn workers ×2",
                          "input": {"agents": ["coder", "tester"]}}),
    (18.0, "agent_start", {"agent": "coder", "name": "Coder", "model": "sonnet", "parent": "orch", "spawned_by": "s5"}),
    (18.0, "agent_start", {"agent": "tester", "name": "Tester", "model": "sonnet", "parent": "orch", "spawned_by": "s5"}),
    (18.0, "step_end", {"step": "s5", "summary": "coder + tester"}),
    (18.0, "step_start", {"step": "s6", "agent": "orch", "title": "Await & report", "status": "queued",
                          "deps": ["s5"]}),
    (18.2, "step_start", {"step": "c1", "agent": "coder", "tool": "Edit", "title": "Edit · session.py",
                          "input": {"file_path": "auth/session.py"}}),
    (18.3, "step_start", {"step": "t1", "agent": "tester", "tool": "Write", "title": "Write test_pkce.py",
                          "input": {"file_path": "tests/test_pkce.py"}}),
    (21.6, "step_end", {"step": "c1", "summary": "+42 −18"}),
    (21.6, "step_start", {"step": "c2", "agent": "coder", "tool": "Write", "title": "Write · pkce.py",
                          "input": {"file_path": "auth/pkce.py"}}),
    (23.4, "step_end", {"step": "t1", "summary": "+88"}),
    (28.4, "step_end", {"step": "c2", "summary": "+96 new"}),
    (28.5, "step_start", {"step": "t2", "agent": "tester", "tool": "Bash", "title": "Bash · pytest",
                          "deps": ["t1", "c2"], "input": {"command": "pytest auth/ -q"}}),
    (39.8, "step_end", {"step": "t2", "status": "error", "summary": "exit 1 · 2 failed",
                        "output": "FAILED test_refresh_boundary\nFAILED test_expiry_with_skew"}),
    (40.0, "message", {"agent": "tester", "to": "orch", "text": "tests failed ×2", "kind": "error"}),
    (40.5, "message", {"agent": "orch", "to": "coder", "text": "fix refresh expiry"}),
    (41.0, "step_start", {"step": "c3", "agent": "coder", "tool": "Edit", "title": "Fix refresh expiry",
                          "deps": ["t2"], "input": {"file_path": "auth/pkce.py",
                                                     "old_string": "expires_at = now + ttl",
                                                     "new_string": "expires_at = now + ttl - SKEW"}}),
    (41.0, "step_start", {"step": "t3", "agent": "tester", "title": "Re-run pytest", "tool": "Bash",
                          "status": "queued", "deps": ["c3"], "input": {"command": "pytest auth/ -q"}}),
    (52.0, "thinking", {"agent": "coder", "text": "Both failures trace to refresh tokens expiring one tick early. "
                        "Adding a clock-skew margin, then re-checking the boundary in is_expired()."}),
    (55.0, "usage", {"agent": "orch", "tokens": {"in": 41200, "out": 3100, "cache_read": 30100}, "cost": 0.31,
                     "context": {"used": 62100, "max": 200000}}),
    (55.0, "usage", {"agent": "coder", "tokens": {"in": 24800, "out": 2900, "cache_read": 19100}, "cost": 0.19,
                     "context": {"used": 41200, "max": 200000}}),
    (55.0, "usage", {"agent": "tester", "tokens": {"in": 13100, "out": 1300, "cache_read": 7000}, "cost": 0.12,
                     "context": {"used": 27900, "max": 200000}}),
    # --- past the mockup snapshot: the run finishes -------------------------
    (58.2, "step_end", {"step": "c3", "summary": "+3 −1"}),
    (58.3, "step_start", {"step": "t3", "agent": "tester", "tool": "Bash", "title": "Re-run pytest"}),
    (66.0, "step_end", {"step": "t3", "summary": "42 passed"}),
    (66.1, "agent_end", {"agent": "coder", "status": "done", "summary": "PKCE implemented"}),
    (66.1, "agent_end", {"agent": "tester", "status": "done", "summary": "42 passed"}),
    (66.2, "step_start", {"step": "s6", "agent": "orch", "title": "Await & report"}),
    (70.5, "step_end", {"step": "s6", "summary": "report sent"}),
    (70.5, "message", {"agent": "orch", "to": "user", "text": "PKCE refactor done · 42 tests passing"}),
    (70.6, "run_end", {"status": "done"}),
]


def build(run_id: str = "demo", base: float | None = None, until: float | None = None) -> list[dict]:
    base = time.time() if base is None else base
    out = []
    for offset, etype, fields in SCRIPT:
        if until is not None and offset > until:
            break
        out.append(events.make(etype, run_id, ts=base + offset, **fields))
    return out


def run(run_id: str = "demo", speed: float = 1.0, instant: bool = False, until: float | None = None,
        path: Path | None = None, echo=print) -> Path:
    from . import paths

    target = path or paths.run_file(run_id)
    if target.exists():
        target.unlink()
    if instant:
        span = until if until is not None else SCRIPT[-1][0]
        for ev in build(run_id, base=time.time() - span, until=until):
            sink.append(ev, target)
        echo(f"wrote {target}")
        return target
    start = time.time()
    echo(f"replaying demo run '{run_id}' into {target} (speed ×{speed}); Ctrl+C to stop")
    for offset, etype, fields in SCRIPT:
        if until is not None and offset > until:
            break
        delay = start + offset / speed - time.time()
        if delay > 0:
            time.sleep(delay)
        sink.append(events.make(etype, run_id, ts=start + offset / speed, **fields), target)
    echo("demo finished")
    return target
