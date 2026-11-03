"""Layout engine shared by both front ends.

Produces *grid* coordinates only (lanes/rows, levels/columns, message rows). The
web UI scales them to pixels; the terminal UI scales them to character cells.

Edge ``kind`` values: ``done`` (finished hand-off), ``active`` (target running),
``pending`` (target queued), ``error`` (source failed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .store import Run, Step

USER = "user"
TOOLS = "tools"


def _edge_kind(src: "Step", dst: "Step") -> str:
    if src.status == "error":
        return "error"
    if dst.status == "queued":
        return "pending"
    if dst.status == "running":
        return "active"
    return "done"


def _edges(run: "Run") -> list[dict]:
    out = []
    for sid in run.order:
        dst = run.steps[sid]
        for dep in dst.deps:
            src = run.steps.get(dep)
            if src is not None:
                out.append({"src": dep, "dst": sid, "kind": _edge_kind(src, dst),
                            "cross": src.agent != dst.agent})
    return out


def lanes(run: "Run") -> list[str]:
    """Agents that own at least one step, in first-seen order (all agents if none do)."""
    with_steps = {s.agent for s in run.steps.values()}
    ids = [a for a in run.agents if a in with_steps]
    return ids or list(run.agents)


def swimlanes(run: "Run") -> dict:
    lane_ids = lanes(run)
    lane_index = {a: i for i, a in enumerate(lane_ids)}
    row: dict[str, int] = {}
    lane_last: dict[str, int] = {}
    for sid in run.order:
        step = run.steps[sid]
        r = lane_last.get(step.agent, -1) + 1
        for dep in step.deps:
            if dep in row:
                r = max(r, row[dep] + 1)
        row[sid] = r
        lane_last[step.agent] = r
    nodes = [{"step": sid, "lane": lane_index.get(run.steps[sid].agent, 0), "row": row[sid]} for sid in run.order]
    return {"lanes": lane_ids, "rows": (max(row.values()) + 1) if row else 0, "nodes": nodes, "edges": _edges(run)}


def critical_path(run: "Run") -> list[str]:
    clock = run.clock()
    best: dict[str, float] = {}
    prev: dict[str, str | None] = {}
    for sid in run.order:
        s = run.steps[sid]
        if s.started is None:
            dur = 0.0
        else:
            dur = max(0.0, (s.ended if s.ended is not None else clock) - s.started)
        top, top_dep = 0.0, None
        for dep in s.deps:
            if dep in best and best[dep] > top:
                top, top_dep = best[dep], dep
        best[sid] = dur + top
        prev[sid] = top_dep
    if not best:
        return []
    cur: str | None = max(best, key=lambda k: best[k])
    path = []
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    return list(reversed(path))


def dag(run: "Run") -> dict:
    lane_index = {a: i for i, a in enumerate(lanes(run))}
    level: dict[str, int] = {}
    for sid in run.order:
        deps = [d for d in run.steps[sid].deps if d in level]
        level[sid] = (max(level[d] for d in deps) + 1) if deps else 0
    by_level: dict[int, list[str]] = {}
    for sid in run.order:
        by_level.setdefault(level[sid], []).append(sid)
    nodes = []
    width = 0
    for lv in sorted(by_level):
        members = sorted(by_level[lv], key=lambda s: (lane_index.get(run.steps[s].agent, 0), run.steps[s].seq))
        width = max(width, len(members))
        for col, sid in enumerate(members):
            nodes.append({"step": sid, "level": lv, "col": col, "ncols": len(members)})
    return {"levels": (max(level.values()) + 1) if level else 0, "width": width, "nodes": nodes,
            "edges": _edges(run), "critical": critical_path(run)}


def sequence(run: "Run") -> dict:
    involved = set()
    for m in run.messages:
        involved.update((m.src, m.dst))
    participants = ([USER] if USER in involved else []) + [a for a in run.agents] + ([TOOLS] if TOOLS in involved else [])
    rows = []
    for m in run.messages:
        kind = m.kind
        if m.step and kind == "call":
            step = run.steps.get(m.step)
            if step is not None and step.status == "running":
                kind = "running"
        rows.append({"ts": m.ts, "src": m.src, "dst": m.dst, "label": m.label, "kind": kind, "step": m.step})
    activations = {}
    for a in run.agents.values():
        idx = [i for i, r in enumerate(rows) if a.id in (r["src"], r["dst"])]
        if idx:
            end = len(rows) - 1 if a.status == "running" else idx[-1]
            activations[a.id] = [idx[0], end]
    return {"participants": participants, "rows": rows, "activations": activations}


def timeline(run: "Run") -> dict:
    clock = run.clock()
    t0 = run.started or clock
    out = []
    for aid in lanes(run):
        bars = []
        for sid in run.order:
            s = run.steps[sid]
            if s.agent != aid:
                continue
            start = s.started if s.started is not None else None
            if start is None:
                bars.append({"step": sid, "start": clock, "end": clock, "status": s.status, "title": s.title})
                continue
            bars.append({"step": sid, "start": start, "end": s.ended if s.ended is not None else clock,
                         "status": s.status, "title": s.title})
        out.append({"agent": aid, "bars": bars})
    return {"t0": t0, "t1": max(clock, t0 + 1.0), "lanes": out}
