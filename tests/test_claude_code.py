"""Tests for the Claude Code hook adapter and hook installer."""

import io
import json

from agentmon import paths, store
from agentmon.hooks import claude_code as cc
from agentmon.tail import RunWatcher

BASE = {"session_id": "sess-1", "transcript_path": "/nope", "cwd": "/home/joe/repos/webapp"}


def payload(name, **fields):
    return {**BASE, "hook_event_name": name, **fields}


def feed(*payloads):
    for p in payloads:
        assert cc.handle_stdin(io.StringIO(json.dumps(p))) == 0
    w = RunWatcher(paths.run_file("sess-1"))
    w.poll()
    return w.run


def test_full_session_maps_to_run():
    run = feed(
        payload("SessionStart", source="startup", model="claude-opus"),
        payload("UserPromptSubmit", prompt="fix the login bug", prompt_id="p1"),
        payload("PreToolUse", tool_name="Grep", tool_input={"pattern": "login"}, tool_use_id="t1"),
        payload("PostToolUse", tool_name="Grep", tool_input={"pattern": "login"}, tool_use_id="t1",
                tool_response={"numFiles": 3}),
        payload("PreToolUse", tool_name="Task", tool_input={"subagent_type": "reviewer"}, tool_use_id="t2"),
        payload("SubagentStart", agent_id="ag-9", agent_type="reviewer"),
        payload("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}, tool_use_id="t3",
                agent_id="ag-9", agent_type="reviewer"),
        payload("PostToolUseFailure", tool_name="Bash", tool_use_id="t3", agent_id="ag-9", error="exit 1"),
        payload("SubagentStop", agent_id="ag-9", last_assistant_message="found 2 issues"),
        payload("PostToolUse", tool_name="Task", tool_use_id="t2", tool_response="found 2 issues"),
        payload("Stop", last_assistant_message="All done."),
        payload("SessionEnd", reason="exit"),
    )
    assert run.title == "webapp"
    assert run.status == "done"
    assert run.agents["main"].model == "claude-opus"
    assert run.steps["t1"].title == "Grep · login" and run.steps["t1"].summary == "3 files"
    assert run.agents["ag-9"].name == "reviewer"
    assert run.agents["ag-9"].spawned_by == "t2"
    assert run.steps["t3"].agent == "ag-9" and run.steps["t3"].status == "error"
    assert run.steps["t3"].deps == ["t2"]
    assert run.errors == 1
    assert run.messages[-1].dst == "user"


def test_hook_never_raises_or_prints(capsys):
    assert cc.handle_stdin(io.StringIO("{garbage")) == 0
    assert cc.handle_stdin(io.StringIO("")) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert (paths.home() / "hook-errors.log").exists()


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("AGENTMON_DISABLE", "1")
    cc.handle_stdin(io.StringIO(json.dumps(payload("SessionStart"))))
    assert not paths.run_file("sess-1").exists()


def test_titles():
    assert cc.step_title("Bash", {"command": "ls -la"}) == "Bash · ls -la"
    assert cc.step_title("Edit", {"file_path": "/a/b/pkce.py"}) == "Edit · pkce.py"
    assert cc.step_title("mcp__github__create_issue", {}) == "create_issue (github)"
    assert cc.step_title("WebFetch", {"url": "https://example.com/x"}) == "WebFetch · example.com"


def test_transcript_usage_dedupes_message_ids(tmp_path):
    t = tmp_path / "t.jsonl"
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}
    lines = [
        {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": usage}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": usage}},
        {"type": "assistant", "message": {"id": "m2", "usage": usage}},
        {"type": "user", "message": {"content": "hi"}},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    got = cc.transcript_usage(str(t))
    assert got["tokens"] == {"in": 20, "out": 10, "cache_read": 200, "cache_write": 0}
    assert got["model"] == "claude-x"
    assert cc.transcript_usage(str(tmp_path / "missing")) is None


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path):
    settings = tmp_path / "home" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard"}]}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [other]}}))
    cc.install("user")
    cc.install("user")
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    pre = data["hooks"]["PreToolUse"]
    assert other in pre
    ours = [e for e in pre if cc._is_ours(e)]
    assert len(ours) == 1 and ours[0]["matcher"] == "*" and ours[0]["hooks"][0]["async"] is True
    assert "async" not in data["hooks"]["SessionStart"][0]["hooks"][0]
    assert settings.with_suffix(".json.agentmon.bak").exists()

    _, removed = cc.uninstall("user")
    assert removed == len(cc.HOOK_EVENTS)
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"PreToolUse": [other]}


def test_install_project_scopes(tmp_path):
    path, _ = cc.install("local", project_dir=tmp_path)
    assert path == tmp_path / ".claude" / "settings.local.json" and path.exists()
    path, _ = cc.install("project", project_dir=tmp_path, dry_run=True)
    assert not path.exists()


def test_reducer_tolerates_async_ordering():
    """Async hooks can land PostToolUse before PreToolUse."""
    run = feed(
        payload("PostToolUse", tool_name="Read", tool_use_id="r1", tool_response="x"),
        payload("PreToolUse", tool_name="Read", tool_input={"file_path": "/a/x.py"}, tool_use_id="r1"),
    )
    assert run.steps["r1"].status == "done" and run.steps["r1"].title == "Read · x.py"
    assert isinstance(run, store.Run)
