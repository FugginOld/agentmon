"""Tests for the FastAPI web server (agentmon[web])."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from agentmon import auth, demo, events, sink  # noqa: E402
from agentmon.web import server  # noqa: E402


def _seed_demo() -> str:
    """Write the scripted demo run to disk; returns its run id."""
    evs = demo.build(until=55)
    for ev in evs:
        sink.append(ev)
    return evs[0]["run"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.create_app(), headers={"Authorization": f"Bearer {auth.load_or_create()}"})


def test_index_and_static(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Agent Monitor" in res.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_runs_empty_then_listed(client):
    assert client.get("/api/runs").json() == []
    run_id = _seed_demo()
    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [run_id]
    assert runs[0]["title"] and runs[0]["size"] > 0 and runs[0]["mtime"] > 0


def test_snapshot_ok_and_404(client):
    run_id = _seed_demo()
    res = client.get(f"/api/runs/{run_id}/snapshot")
    assert res.status_code == 200
    snap = res.json()
    assert snap["run"]["id"] == run_id
    assert {"agents", "steps", "metrics", "log", "focus", "layouts"} <= snap.keys()
    assert set(snap["layouts"]) == {"swim", "dag", "seq", "timeline"}
    assert client.get("/api/runs/nope/snapshot").status_code == 404
    # ids are never treated as file paths
    assert client.get("/api/runs/..%2f..%2fetc%2fpasswd/snapshot").status_code == 404


def test_post_valid_event_visible_in_snapshot(client):
    body = [
        {"type": "run_start", "run": "http-run", "title": "from http"},
        {"type": "agent_start", "run": "http-run", "agent": "a1", "name": "Agent One"},
        {"type": "step_start", "run": "http-run", "agent": "a1", "step": "x1", "title": "Think"},
    ]
    res = client.post("/api/events", json=body)
    assert res.status_code == 200 and res.json() == {"accepted": 3}
    snap = client.get("/api/runs/http-run/snapshot").json()
    assert snap["run"]["title"] == "from http"
    assert [s["id"] for s in snap["steps"]] == ["x1"]
    # a single object is accepted too, and v/ts are filled in
    one = client.post("/api/events", json={"type": "step_end", "run": "http-run", "step": "x1"})
    assert one.json() == {"accepted": 1}
    line = json.loads(client.get("/api/runs/http-run/events.jsonl").text.splitlines()[-1])
    assert line["v"] == events.SCHEMA_VERSION and "ts" in line


@pytest.mark.parametrize("payload", [
    {"type": "bogus", "run": "r"},
    {"type": "step_start", "run": "r", "agent": "a"},        # missing step
    {"run": "r"},                                             # missing type
    [],                                                       # empty batch
    "just a string",
])
def test_post_invalid_is_400(client, payload):
    res = client.post("/api/events", json=payload)
    assert res.status_code == 400
    assert res.json()["error"]


def test_post_bad_json_and_content_type(client):
    assert client.post("/api/events", content=b"{nope", headers={"content-type": "application/json"}).status_code == 400
    # text/plain is refused so cross-site pages can't write events without a CORS preflight
    assert client.post("/api/events", content=b"{}", headers={"content-type": "text/plain"}).status_code == 415


def test_bad_batch_writes_nothing(client):
    good = {"type": "run_start", "run": "atomic"}
    res = client.post("/api/events", json=[good, {"type": "nope", "run": "atomic"}])
    assert res.status_code == 400
    assert client.get("/api/runs").json() == []


def test_events_download(client):
    run_id = _seed_demo()
    res = client.get(f"/api/runs/{run_id}/events.jsonl")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    lines = [json.loads(x) for x in res.text.splitlines()]
    assert lines[0]["type"] == "run_start"
    assert client.get("/api/runs/nope/events.jsonl").status_code == 404


def _collect(run, frames, **kw):
    """Drive the SSE generator until `frames` frames arrived, then disconnect."""
    async def go():
        got: list[str] = []

        async def disconnected() -> bool:
            return len(got) >= frames

        async for chunk in server.stream_snapshots(run, disconnected, poll_interval=0.01, **kw):
            got.append(chunk)
        return got
    return asyncio.run(asyncio.wait_for(go(), 5))


def _parse(frame: str) -> tuple[str, dict]:
    name, data = frame.strip().split("\n", 1)
    return name.removeprefix("event: "), json.loads(data.removeprefix("data: "))


def test_stream_empty_when_no_runs():
    name, _ = _parse(_collect("latest", 1)[0])
    assert name == "empty"


def test_stream_first_frame_is_snapshot():
    run_id = _seed_demo()
    name, snap = _parse(_collect("latest", 1)[0])
    assert name == "snapshot" and snap["run"]["id"] == run_id
    name, snap = _parse(_collect(run_id, 1)[0])
    assert name == "snapshot" and snap["run"]["id"] == run_id


def test_stream_pushes_new_events_and_unknown_run_is_empty():
    run_id = _seed_demo()

    async def go():
        seen: list[dict] = []
        wrote = False

        async def disconnected() -> bool:
            nonlocal wrote
            if len(seen) == 1 and not wrote:
                wrote = True
                sink.append(events.make("note", run_id, text="late event"))
            return len(seen) >= 2

        async for chunk in server.stream_snapshots(run_id, disconnected, poll_interval=0.01):
            seen.append(_parse(chunk)[1])
        return seen

    first, second = asyncio.run(asyncio.wait_for(go(), 5))
    assert second["run"]["events"] == first["run"]["events"] + 1
    assert _parse(_collect("missing", 1)[0])[0] == "empty"


def test_stream_endpoint_registered(client):
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert {"/api/stream", "/api/events", "/api/runs"} <= paths
