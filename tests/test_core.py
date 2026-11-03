"""Tests for the event schema, reducer, layouts, file tailing and demo script."""

import json
import time

import pytest

from agentmon import demo, events, layout, paths, sink, store
from agentmon.tail import RunWatcher, list_runs, resolve


def ev(etype, **fields):
    return events.make(etype, "r1", **fields)


# ----------------------------------------------------------------------- events
def test_make_and_validate_roundtrip():
    e = ev("step_start", step="a", agent="x", title="Do thing")
    assert e["v"] == 1 and e["type"] == "step_start"
    assert events.parse_line(json.dumps(e)) == e


@pytest.mark.parametrize("bad", [
    {"type": "nope", "run": "r"},
    {"type": "step_start", "run": "r", "agent": "a"},           # missing step
    {"type": "message", "run": "", "agent": "a", "text": "x"},  # empty run
    {"type": "step_end", "run": "r", "step": "s", "status": "weird"},
    {"type": "step_start", "run": "r", "step": "s", "agent": "a", "deps": "s0"},
    {"type": "note", "run": "r", "text": "x", "ts": "not-a-time"},
])
def test_validate_rejects(bad):
    with pytest.raises(events.EventError):
        events.validate(bad)


def test_parse_line_ignores_garbage():
    assert events.parse_line("") is None
    assert events.parse_line("{not json") is None
    assert events.parse_line('{"type":"nope","run":"r"}') is None


def test_parse_ts_accepts_epoch_and_iso():
    assert events.parse_ts(10) == 10.0
    assert events.parse_ts("1970-01-01T00:00:10Z") == pytest.approx(10.0)


# ---------------------------------------------------------------------- reducer
def test_deps_inferred_within_agent_and_from_spawn():
    run = store.load([
        ev("agent_start", agent="lead"),
        ev("step_start", step="s1", agent="lead", tool="Agent", title="Spawn helper",
           input={"subagent_type": "helper"}),
        ev("agent_start", agent="h1", name="helper", kind="helper", parent="lead"),
        ev("step_start", step="h-a", agent="h1", title="first"),
        ev("step_start", step="h-b", agent="h1", title="second"),
    ])
    assert run.agents["h1"].spawned_by == "s1"
    assert run.steps["h-a"].deps == ["s1"]
    assert run.steps["h-b"].deps == ["h-a"]


def test_out_of_order_end_before_start_does_not_downgrade():
    run = store.load([
        ev("step_end", step="x", agent="a", tool="Bash", summary="ok"),
        ev("step_start", step="x", agent="a", tool="Bash", title="Bash · ls"),
    ])
    step = run.steps["x"]
    assert step.status == "done"
    assert step.title == "Bash · ls"
    assert len(run.order) == 1


def test_queued_step_promotes_to_running_and_records_call():
    run = store.load([
        ev("step_start", step="q", agent="a", tool="Bash", title="later", status="queued"),
    ])
    assert run.steps["q"].status == "queued" and run.steps["q"].started is None
    assert not run.messages
    run.apply(ev("step_start", step="q", agent="a"))
    assert run.steps["q"].status == "running"
    assert run.messages[-1].kind == "call"


def test_errors_counted_and_usage_overrides_step_tokens():
    run = store.load([
        ev("step_start", step="s", agent="a", title="t"),
        ev("step_end", step="s", status="error", tokens={"in": 5, "out": 1}),
    ])
    assert run.metrics()["errors"] == 1
    assert run.agent_tokens("a") == {"in": 5, "out": 1}
    run.apply(ev("usage", agent="a", tokens={"in": 100, "out": 10, "cache_read": 300}, cost=0.5))
    m = run.metrics()
    assert m["tokens"]["in"] == 100 and m["cost"] == 0.5
    assert m["cache_hit"] == pytest.approx(300 / 400)


def test_clock_freezes_when_run_ends():
    t = time.time() - 100
    run = store.load([
        events.make("run_start", "r1", ts=t),
        events.make("run_end", "r1", ts=t + 30),
    ])
    assert run.clock() == pytest.approx(t + 30)
    assert run.metrics()["elapsed"] == pytest.approx(30)


def test_snapshot_is_json_serialisable():
    run = store.load(demo.build("d", until=55))
    snap = run.snapshot()
    json.dumps(snap)
    assert snap["focus"] == "c3"
    assert snap["metrics"]["steps"]["running"] == 1


# ---------------------------------------------------------------------- layouts
def test_swimlane_rows_respect_deps_and_lanes():
    run = store.load(demo.build("d", until=55))
    sw = layout.swimlanes(run)
    rows = {n["step"]: n["row"] for n in sw["nodes"]}
    for e in sw["edges"]:
        assert rows[e["dst"]] > rows[e["src"]]
    assert sw["lanes"] == ["orch", "explorer", "coder", "tester"]
    kinds = {(e["src"], e["dst"]): e["kind"] for e in sw["edges"]}
    assert kinds[("t2", "c3")] == "error"
    assert kinds[("c3", "t3")] == "pending"


def test_dag_levels_and_critical_path():
    run = store.load(demo.build("d", until=55))
    d = layout.dag(run)
    lv = {n["step"]: n["level"] for n in d["nodes"]}
    for e in d["edges"]:
        assert lv[e["dst"]] > lv[e["src"]]
    assert d["critical"][0] == "s1" and d["critical"][-1] == "c3"


def test_sequence_marks_running_call():
    run = store.load(demo.build("d", until=55))
    seq = layout.sequence(run)
    assert seq["participants"][0] == "user" and seq["participants"][-1] == "tools"
    assert any(r["kind"] == "running" and r["step"] == "c3" for r in seq["rows"])
    assert seq["activations"]["coder"][1] == len(seq["rows"]) - 1


def test_completed_demo_run():
    run = store.load(demo.build("d"))
    assert run.status == "done"
    assert all(s.status in ("done", "error") for s in run.steps.values())
    assert all(a.status == "done" for a in run.agents.values() if a.id != "orch")


# ------------------------------------------------------------------- sink/tail
def test_sink_and_watcher_incremental():
    path = paths.run_file("r1")
    w = RunWatcher(path)
    assert w.poll() is False
    sink.append(ev("run_start", title="hello"))
    assert w.poll() is True and w.run.title == "hello"
    with open(path, "a") as fh:  # partial line is held until completed
        fh.write('{"v":1,"run":"r1","type":"note",')
    assert w.poll() is False
    with open(path, "a") as fh:
        fh.write('"text":"done"}\n')
    assert w.poll() is True
    assert w.run.log[-1]["text"] == "done"


def test_watcher_restarts_on_truncate():
    sink.append(ev("run_start", title="a"))
    path = paths.run_file("r1")
    w = RunWatcher(path)
    w.poll()
    path.write_text("")
    sink.append(ev("run_start", title="b"))
    w.poll()
    assert w.run.title == "b" and w.run.event_count == 1


def test_list_and_resolve_runs():
    sink.append(events.make("run_start", "older", title="Old"))
    time.sleep(0.01)
    sink.append(events.make("run_start", "newer", title="New"))
    import os
    os.utime(paths.run_file("newer"), (time.time() + 5, time.time() + 5))
    runs = list_runs()
    assert [r.id for r in runs] == ["newer", "older"]
    assert resolve(None) == paths.run_file("newer")
    assert resolve("old") == paths.run_file("older")
    assert resolve("missing") is None


def test_safe_run_ids():
    assert paths.safe_id("../../etc/passwd") == "etc-passwd"
    assert paths.run_file("a/b").parent == paths.runs_dir()
