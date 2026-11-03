"""CLI behaviour that doesn't need either UI installed."""

import io
import json
import sys

from agentmon import cli, paths
from agentmon.tail import RunWatcher


def test_emit_with_fields():
    assert cli.main(["emit", "run_start", "--run", "x", "--set", "title=Hello"]) == 0
    assert cli.main(["emit", "step_start", "--run", "x", "--json", '{"step": "s", "agent": "a", "deps": []}']) == 0
    w = RunWatcher(paths.run_file("x"))
    w.poll()
    assert w.run.title == "Hello" and "s" in w.run.steps


def test_emit_rejects_invalid(capsys):
    assert cli.main(["emit", "step_start", "--run", "x"]) == 2
    assert "invalid event" in capsys.readouterr().err


def test_emit_stdin_jsonl(monkeypatch):
    lines = "\n".join(json.dumps(e) for e in [
        {"type": "run_start", "title": "piped"},
        {"type": "note", "text": "hi"},
        {"type": "bogus"},
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines + "\n"))
    assert cli.main(["emit", "-", "--run", "piped"]) == 1  # one bad line
    w = RunWatcher(paths.run_file("piped"))
    w.poll()
    assert w.run.title == "piped" and w.run.event_count == 2


def test_demo_instant_and_runs(capsys):
    assert cli.main(["demo", "--instant", "--until", "20"]) == 0
    assert paths.run_file("demo").exists()
    assert cli.main(["runs"]) == 0
    assert "auth-pkce-refactor" in capsys.readouterr().out


def test_install_hooks_dry_run(capsys):
    assert cli.main(["install-hooks", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "agentmon hook" in out and "dry run" in out


def test_help_without_command(capsys):
    assert cli.main([]) == 0
    assert "install-hooks" in capsys.readouterr().out
