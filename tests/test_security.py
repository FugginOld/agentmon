"""Regressions for the repo's code-scanning alerts: NaN/Infinity, error echo, file permissions."""

from __future__ import annotations

import json
import math
import sys

import pytest

from agentmon import cli, events, paths, sink


# --------------------------------------------------------------------------- NaN / Infinity
@pytest.mark.parametrize("ts", [math.nan, math.inf, -math.inf, True])
def test_non_finite_or_boolean_timestamps_are_rejected(ts):
    with pytest.raises(events.EventError):
        events.validate({"type": "note", "run": "r", "text": "x", "ts": ts})


@pytest.mark.parametrize("text", ['{"ts": NaN}', '{"a": {"b": [1, Infinity]}}', '-Infinity'])
def test_loads_refuses_non_standard_constants_anywhere(text):
    with pytest.raises(events.EventError) as err:
        events.loads(text)
    assert err.value.code == "non_finite"
    assert events.loads('{"n": 1.5, "s": "NaN"}') == {"n": 1.5, "s": "NaN"}  # ordinary JSON still parses


def test_a_run_file_line_with_nan_is_skipped_not_fatal():
    good = '{"type":"note","run":"r","text":"ok"}'
    assert events.parse_line('{"type":"usage","run":"r","agent":"a","cost":NaN}') is None
    assert events.parse_line(good)["text"] == "ok"


def test_sink_refuses_nan_and_writes_nothing():
    bad = events.make("usage", "nanrun", agent="a", cost=float("nan"))
    with pytest.raises(events.EventError):
        sink.append(bad)
    assert not paths.run_file("nanrun").exists()


def test_emit_cli_reports_nan_as_an_invalid_event(capsys):
    assert cli.main(["emit", "usage", "--run", "x", "--set", "agent=a", "--set", "cost=NaN"]) == 2
    assert "invalid event" in capsys.readouterr().err and not paths.run_file("x").exists()


# --------------------------------------------------------------------------- the HTTP ingest
@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from agentmon.web import server

    from agentmon import auth

    return TestClient(server.create_app(), headers={"Authorization": f"Bearer {auth.load_or_create()}"})


JSON = {"content-type": "application/json"}


@pytest.mark.parametrize("body", [
    b'{"type":"note","run":"web-nan","text":"x","ts":NaN}',
    b'{"type":"usage","run":"web-nan","agent":"a","tokens":{"in":Infinity}}',
])
def test_post_with_nan_is_rejected_and_nothing_is_written(client, body):
    res = client.post("/api/events", content=body, headers=JSON)
    assert res.status_code == 400 and not paths.run_file("web-nan").exists()
    assert client.get("/api/runs").status_code == 200


def test_post_error_does_not_echo_the_senders_input(client):
    res = client.post("/api/events", content=json.dumps({"type": "<script>alert(1)</script>", "run": "r"}),
                      headers=JSON)
    assert res.status_code == 400
    assert res.json()["code"] == "unknown_type" and "script" not in res.text
    res = client.post("/api/events", content=b'{"type":"note","run":"r","text":"x","ts":"not-a-time"}', headers=JSON)
    assert res.json()["code"] == "bad_ts" and "not-a-time" not in res.text


def test_every_error_code_the_schema_raises_has_a_public_reason():
    for code in ("not_object", "unknown_type", "bad_run", "missing_field", "bad_ts", "bad_status", "bad_deps",
                 "non_finite"):
        assert code in events.REASONS
    samples = [[], {"type": "x", "run": "r"}, {"type": "note", "run": ""}, {"type": "step_end", "run": "r"},
               {"type": "note", "run": "r", "text": "t", "ts": "?"}, {"type": "step_end", "run": "r", "step": "s", "status": "?"},
               {"type": "step_end", "run": "r", "step": "s", "deps": [1]}]
    seen = set()
    for sample in samples:
        with pytest.raises(events.EventError) as err:
            events.validate(sample)
        seen.add(err.value.code)
        assert err.value.code in events.REASONS
    assert seen == set(events.REASONS) - {"non_finite"}


# --------------------------------------------------------------------------- permissions
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_new_run_logs_and_folders_are_owner_only():
    import stat

    path = sink.append(events.make("note", "perm", text="secret prompt"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.home().stat().st_mode) == 0o700
