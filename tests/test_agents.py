"""Hook adapters for Codex, Qwen Code, Gemini CLI and Kimi CLI. Payloads follow each CLI's hooks reference."""

import io
import json
import sys

import pytest

from agentmon import cli, paths
from agentmon.hooks import agents, pairing
from agentmon.hooks import claude_code as cc
from agentmon.tail import RunWatcher


def send(agent, run_id, *payloads):
    for p in payloads:
        raw = json.dumps({"session_id": run_id, "cwd": "/home/joe/repos/webapp", **p})
        assert agents.handle(agent, io.StringIO(raw)) == 0
    w = RunWatcher(paths.run_file(run_id))
    w.poll()
    return w.run


def first_event(run_id):
    return json.loads(paths.run_file(run_id).read_text().splitlines()[0])


# --------------------------------------------------------------------------- Codex / Qwen
def test_codex_session_and_token_totals():
    run = send(
        "codex", "cx",
        {"hook_event_name": "SessionStart", "source": "startup", "model": "gpt-5-codex"},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "pytest -q"}},
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t1", "tool_response": "3 passed"},
        {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_use_id": "t2",
         "tool_input": {"command": "*** Begin Patch\n*** Update File: src/app/main.py\n@@\n"}},
        {"hook_event_name": "PostModelRequest", "status": "completed", "token_usage": {"input_tokens": 100, "output_tokens": 10}},
        {"hook_event_name": "PostModelRequest", "status": "completed",
         "token_usage": {"input_tokens": 50, "output_tokens": 5, "cached_input_tokens": 20}},
        {"hook_event_name": "SessionEnd", "reason": "other"},
    )
    assert first_event("cx")["source"] == "codex:startup"
    assert run.agents["main"].name == "Codex" and run.agents["main"].model == "gpt-5-codex"
    assert run.steps["t1"].status == "done" and run.steps["t1"].title == "Bash · pytest -q"
    assert run.steps["t2"].title == "apply_patch · main.py"
    # usage events carry absolute totals: the second request must add to the first, not replace it
    assert run.agent_tokens("main")["in"] == 150 and run.agent_tokens("main")["out"] == 15
    assert run.agent_tokens("main")["cache_read"] == 20
    assert run.status == "done"


def test_qwen_session_uses_shared_mapping():
    run = send(
        "qwen", "qw",
        {"hook_event_name": "SessionStart", "source": "startup"},
        {"hook_event_name": "PreToolUse", "tool_name": "run_shell_command", "tool_use_id": "q1",
         "tool_input": {"command": "npm test"}},
        {"hook_event_name": "PostToolUseFailure", "tool_name": "run_shell_command", "tool_use_id": "q1", "error": "exit 1"},
    )
    assert first_event("qw")["source"] == "qwen-code:startup"
    assert run.agents["main"].name == "Qwen"
    assert run.steps["q1"].status == "error" and run.steps["q1"].title == "run_shell_command · npm test"


# --------------------------------------------------------------------------- Gemini / Kimi (paired steps)
def test_gemini_session():
    shell = {"command": "npm test"}
    run = send(
        "gemini", "gm",
        {"hook_event_name": "SessionStart", "source": "startup"},
        {"hook_event_name": "BeforeAgent", "prompt": "fix the build"},
        {"hook_event_name": "BeforeTool", "tool_name": "run_shell_command", "tool_input": shell},
        {"hook_event_name": "AfterTool", "tool_name": "run_shell_command", "tool_input": shell,
         "tool_response": {"llmContent": "ok", "returnDisplay": "3 passed"}},
        {"hook_event_name": "BeforeTool", "tool_name": "read_file", "tool_input": {"absolute_path": "/a/b.py"}},
        {"hook_event_name": "AfterTool", "tool_name": "read_file", "tool_input": {"absolute_path": "/a/b.py"},
         "tool_response": {"error": {"message": "ENOENT"}}},
        {"hook_event_name": "AfterAgent", "prompt_response": "Done."},
        {"hook_event_name": "SessionEnd", "reason": "exit"},
    )
    assert first_event("gm")["source"] == "gemini-cli:startup"
    done = [s for s in run.steps.values() if s.tool == "run_shell_command"]
    assert len(done) == 1 and done[0].status == "done" and done[0].summary == "3 passed"
    bad = [s for s in run.steps.values() if s.tool == "read_file"]
    assert bad[0].status == "error" and bad[0].title == "read_file · b.py"
    assert run.messages[-1].dst == "user" and run.status == "done"
    assert not (paths.home() / "state" / "gm.json").exists()  # SessionEnd cleans up the pairing state


def test_identical_calls_stay_separate_steps():
    """A hash-only id would merge these; pairing must give every call its own step."""
    same = {"command": "ls"}
    pre = {"hook_event_name": "BeforeTool", "tool_name": "run_shell_command", "tool_input": same}
    post = {"hook_event_name": "AfterTool", "tool_name": "run_shell_command", "tool_input": same,
            "tool_response": {"returnDisplay": "x"}}
    run = send("gemini", "dup", {"hook_event_name": "SessionStart"}, pre, pre, post, post)
    steps = [s for s in run.steps.values() if s.tool == "run_shell_command"]
    assert len(steps) == 2 and all(s.status == "done" for s in steps)


def test_after_without_before_still_renders():
    run = send("gemini", "orphan", {"hook_event_name": "SessionStart"},
               {"hook_event_name": "AfterTool", "tool_name": "glob", "tool_input": {"pattern": "*.py"},
                "tool_response": {"returnDisplay": "2 files"}})
    assert [s.status for s in run.steps.values()] == ["done"]


def test_kimi_session_with_subagent():
    cmd = {"command": "pytest"}
    run = send(
        "kimi", "km",
        {"hook_event_name": "SessionStart", "source": "startup"},
        {"hook_event_name": "UserPromptSubmit", "prompt": "run the tests"},
        {"hook_event_name": "PreToolUse", "tool_name": "Shell", "tool_input": cmd, "tool_call_id": "c1"},
        {"hook_event_name": "PostToolUse", "tool_name": "Shell", "tool_input": cmd, "tool_output": "5 passed"},
        {"hook_event_name": "PreToolUse", "tool_name": "Shell", "tool_input": {"command": "false"}},
        {"hook_event_name": "PostToolUseFailure", "tool_name": "Shell", "tool_input": {"command": "false"}, "error": "exit 1"},
        {"hook_event_name": "SubagentStart", "agent_name": "explorer", "prompt": "look around"},
        {"hook_event_name": "SubagentStop", "agent_name": "explorer", "response": "found it"},
        {"hook_event_name": "StopFailure", "error_type": "rate_limit", "error_message": "429"},
        {"hook_event_name": "SessionEnd", "reason": "exit"},
    )
    assert first_event("km")["source"] == "kimi-cli:startup"
    by_title = {s.title: s for s in run.steps.values() if s.tool == "Shell"}
    assert by_title["Shell · pytest"].status == "done" and by_title["Shell · pytest"].summary == "5 passed"
    assert by_title["Shell · false"].status == "error"
    assert run.agents["explorer"].parent == "main"
    assert run.errors >= 2 and run.status == "done"


def test_pairing_closes_oldest_first():
    a, b = pairing.begin("r", "T", {"x": 1}), pairing.begin("r", "T", {"x": 1})
    assert a != b
    assert pairing.end("r", "T", {"x": 1}) == a and pairing.end("r", "T", {"x": 1}) == b


# --------------------------------------------------------------------------- stdout contract
@pytest.mark.parametrize("agent,expected", [("claude", ""), ("qwen", ""), ("kimi", ""), ("codex", "{}"), ("gemini", "{}")])
def test_stdout_contract(agent, expected, capsys, monkeypatch):
    assert agents.handle(agent, io.StringIO("{garbage")) == 0  # bad input: still exits 0
    assert capsys.readouterr().out == expected
    monkeypatch.setenv("AGENTMON_DISABLE", "1")
    assert agents.handle(agent, io.StringIO("{}")) == 0
    assert capsys.readouterr().out == expected


# --------------------------------------------------------------------------- installers
FOREIGN = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard"}]}


@pytest.mark.parametrize("agent,cfg", [("codex", ".codex/hooks.json"), ("qwen", ".qwen/settings.json"),
                                       ("gemini", ".gemini/settings.json")])
def test_json_install_is_idempotent_and_preserves_other_hooks(agent, cfg, isolated_home):
    path = isolated_home / "home" / cfg
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [FOREIGN], "BeforeTool": [FOREIGN]}}))
    agents.install(agent)
    agents.install(agent)
    data = json.loads(path.read_text())
    assert data["model"] == "x"
    ours = [e for ev in data["hooks"].values() for e in ev if cc._is_ours(e)]
    assert len(ours) == len(agents.TARGETS[agent].events)  # one per event, none duplicated
    assert all(f"--agent {agent}" in e["hooks"][0]["command"] and "matcher" not in e for e in ours)
    for ev in ("PreToolUse", "BeforeTool"):
        if ev in data["hooks"]:
            assert FOREIGN in data["hooks"][ev]
    _, removed = agents.uninstall(agent)
    assert removed == len(ours)
    data = json.loads(path.read_text())
    assert data["hooks"] == {k: [FOREIGN] for k in ("PreToolUse", "BeforeTool")}


def test_per_cli_timeouts_and_async(isolated_home):
    agents.install("gemini")
    agents.install("codex")
    gem = json.loads((isolated_home / "home" / ".gemini" / "settings.json").read_text())["hooks"]
    assert gem["BeforeTool"][0]["hooks"][0]["timeout"] == 10000  # Gemini counts milliseconds
    assert all("async" not in e["hooks"][0] for ev in gem.values() for e in ev)  # Gemini hooks are sync-only
    cdx = json.loads((isolated_home / "home" / ".codex" / "hooks.json").read_text())["hooks"]
    assert cdx["PreToolUse"][0]["hooks"][0]["async"] is True and cdx["PreToolUse"][0]["hooks"][0]["timeout"] == 10
    assert "async" not in cdx["SessionEnd"][0]["hooks"][0]


def test_claude_command_is_unchanged(isolated_home):
    assert cc.hook_command().endswith("-m agentmon hook")
    assert cc.hook_command("codex").endswith("-m agentmon hook --agent codex")


KIMI_ORIGINAL = 'default_model = "kimi-k2"\n\n[loop_control]\nmax_steps = 50\n'


def test_kimi_toml_roundtrip_is_byte_identical(isolated_home):
    path = isolated_home / "home" / ".kimi" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(KIMI_ORIGINAL.encode())
    agents.install("kimi")
    agents.install("kimi")
    text = path.read_text()
    assert text.count("[[hooks]]") == len(agents.KIMI_EVENTS) and text.startswith(KIMI_ORIGINAL)
    try:
        import tomllib
    except ImportError:  # Python 3.10
        tomllib = None
    if tomllib:
        hooks = tomllib.loads(text)["hooks"]
        assert {h["event"] for h in hooks} == set(agents.KIMI_EVENTS)
        assert all(set(h) == {"event", "command", "timeout"} for h in hooks)  # Kimi rejects unknown keys
        assert "--agent kimi" in hooks[0]["command"]
    _, removed = agents.uninstall("kimi")
    assert removed == len(agents.KIMI_EVENTS)
    assert path.read_bytes() == KIMI_ORIGINAL.encode()


def test_kimi_keeps_crlf_files_crlf(isolated_home):
    path = isolated_home / "home" / ".kimi" / "config.toml"
    path.parent.mkdir(parents=True)
    original = KIMI_ORIGINAL.replace("\n", "\r\n").encode()
    path.write_bytes(original)
    agents.install("kimi")
    assert b"\r\n" in path.read_bytes() and b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    agents.uninstall("kimi")
    assert path.read_bytes() == original


def test_kimi_install_creates_missing_config_and_uninstalls_to_empty(isolated_home):
    path, _, _ = agents.install("kimi")
    assert path.exists()
    agents.uninstall("kimi")
    assert path.read_text() == ""


def test_scope_is_validated(capsys):
    assert cli.main(["install-hooks", "--agent", "kimi", "--scope", "project"]) == 2
    assert "no 'project' scope" in capsys.readouterr().err
    assert cli.main(["install-hooks", "--agent", "gemini", "--scope", "local"]) == 2


def test_cli_hook_agent_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "viacli"})))
    assert cli.main(["hook", "--agent", "codex"]) == 0
    assert capsys.readouterr().out == "{}"
    assert first_event("viacli")["source"] == "codex:startup"


def test_install_message_names_the_cli(capsys):
    assert cli.main(["install-hooks", "--agent", "codex"]) == 0
    out = capsys.readouterr().out
    assert "Codex events" in out and "/hooks" in out


def test_titles_for_other_clis():
    assert cc.step_title("ReadFile", {"path": "/x/y/z.py"}) == "ReadFile · z.py"
    assert cc.step_title("run_shell_command", {"command": "ls -la"}) == "run_shell_command · ls -la"
    assert cc.step_title("apply_patch", {"command": "no file header"}) == "apply_patch"
    assert cc.step_title("Mystery", {}) == "Mystery"


# --------------------------------------------------------------------------- review regressions
def _kimi_cfg(isolated_home, text):
    path = isolated_home / "home" / ".kimi" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode())
    return path


def test_kimi_config_with_its_own_empty_hooks_array(isolated_home):
    """Kimi saves `hooks = []`; `[[hooks]]` tables after that are invalid TOML, so install must park it."""
    original = 'default_model = "kimi-k2"\nhooks = []\n\n[loop_control]\nmax_steps = 50\n'
    path = _kimi_cfg(isolated_home, original)
    agents.install("kimi")
    text = path.read_text()
    try:
        import tomllib
    except ImportError:  # Python 3.10
        tomllib = None
    if tomllib:
        parsed = tomllib.loads(text)  # raises if `hooks = []` and `[[hooks]]` coexist
        assert len(parsed["hooks"]) == len(agents.KIMI_EVENTS) and parsed["loop_control"] == {"max_steps": 50}
    assert "\nhooks = []" not in text
    agents.uninstall("kimi")
    assert path.read_bytes() == original.encode()


def test_kimi_refuses_a_non_empty_top_level_hooks_array(isolated_home, capsys):
    original = 'hooks = [{event = "Stop", command = "x"}]\n'
    path = _kimi_cfg(isolated_home, original)
    assert cli.main(["install-hooks", "--agent", "kimi"]) == 2
    assert "[[hooks]] tables" in capsys.readouterr().err
    assert path.read_bytes() == original.encode()  # untouched


def test_kimi_hooks_key_inside_a_table_is_not_top_level(isolated_home):
    original = '[plugins]\nhooks = []\n'
    path = _kimi_cfg(isolated_home, original)
    agents.install("kimi")
    assert "[plugins]\nhooks = []\n" in path.read_text()
    agents.uninstall("kimi")
    assert path.read_bytes() == original.encode()


def test_json_installer_keeps_utf8_and_crlf(isolated_home):
    path = isolated_home / "home" / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"theme": "Café — dark", "hooks": {}}, indent=2, ensure_ascii=False).replace("\n", "\r\n")
    path.write_bytes(original.encode("utf-8"))
    agents.install("gemini")
    raw = path.read_bytes()
    assert "Café — dark".encode("utf-8") in raw  # not mojibake from a locale codec
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert path.with_suffix(".json.agentmon.bak").read_bytes() == original.encode("utf-8")
    agents.uninstall("gemini")
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "Café — dark"


def test_commented_json_settings_get_a_clear_error_and_are_left_alone(isolated_home, capsys):
    path = isolated_home / "home" / ".qwen" / "settings.json"
    path.parent.mkdir(parents=True)
    original = '{\n  // my notes\n  "model": "x"\n}\n'
    path.write_text(original)
    assert cli.main(["install-hooks", "--agent", "qwen"]) == 2
    assert "not plain JSON" in capsys.readouterr().err
    assert path.read_text() == original and not path.with_suffix(".json.agentmon.bak").exists()


def test_codex_session_end_timeout_is_within_codexs_limit(isolated_home):
    agents.install("codex")
    hooks = json.loads((isolated_home / "home" / ".codex" / "hooks.json").read_text())["hooks"]
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 3  # Codex allows SessionEnd at most 3 s
    assert hooks["PreToolUse"][0]["hooks"][0]["timeout"] == 10


def test_pairing_trouble_does_not_cost_the_event(monkeypatch):
    def broken(*a, **k):
        raise OSError("state dir is read-only")

    monkeypatch.setattr(pairing, "_save", broken)
    run = send("gemini", "ro", {"hook_event_name": "SessionStart"},
               {"hook_event_name": "BeforeTool", "tool_name": "glob", "tool_input": {"pattern": "*"}},
               {"hook_event_name": "AfterTool", "tool_name": "glob", "tool_input": {"pattern": "*"},
                "tool_response": {"returnDisplay": "1 file"}})
    assert [s.status for s in run.steps.values()] == ["done"]  # start and end meet on the deterministic fallback id
    assert not (paths.home() / "hook-errors.log").exists()


def test_codex_usage_survives_a_broken_state_file(monkeypatch):
    monkeypatch.setattr(pairing, "_save", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    run = send("codex", "cxro", {"hook_event_name": "SessionStart"},
               {"hook_event_name": "PostModelRequest", "token_usage": {"input_tokens": 5}},
               {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": "ls"}})
    assert "t" in run.steps  # the tool event after the failed usage update is still recorded


def test_pairing_survives_parallel_hook_processes(isolated_home):
    """Gemini/Kimi/Codex hooks overlap; 5 processes x 12 begin() calls must lose nothing and reuse no id."""
    import os
    import subprocess
    from pathlib import Path

    code = ("from agentmon.hooks import pairing\n"
            "for _ in range(12): pairing.begin('race', 'T', {'x': 1})\n")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.PIPE) for _ in range(5)]
    assert [p.wait(timeout=60) for p in procs] == [0] * 5, [p.stderr.read() for p in procs]
    state = pairing._load("race")
    ids = [i for queue in state["open"].values() for i in queue]
    assert len(ids) == 60 and len(set(ids)) == 60 and state["seq"] == 60


# --------------------------------------------------------------------------- second-review regressions
def _toml_ok(text):
    try:
        import tomllib
    except ImportError:  # Python 3.10
        return None
    return tomllib.loads(text)


def test_kimi_uninstall_keeps_hooks_parked_while_user_hook_tables_remain(isolated_home):
    """Restoring `hooks = []` next to the user's own [[hooks]] tables would make the config invalid again."""
    path = _kimi_cfg(isolated_home, 'default_model = "k"\nhooks = []\n')
    agents.install("kimi")
    path.write_text(path.read_text() + '\n[[hooks]]\nevent = "Stop"\ncommand = "mine"\n')
    agents.uninstall("kimi")
    text = path.read_text()
    parsed = _toml_ok(text)  # raises if `hooks = []` and `[[hooks]]` coexist
    if parsed:
        assert [h["command"] for h in parsed["hooks"]] == ["mine"]
    assert "agentmon" not in text.replace("agentmon-off", "")


def test_kimi_parks_indented_and_commented_empty_hooks(isolated_home):
    for original in ('default_model = "k"\n  hooks = []\n', 'hooks = [] # none yet\nx = 1\n'):
        path = _kimi_cfg(isolated_home, original) if not (isolated_home / "home" / ".kimi").exists() else (
            isolated_home / "home" / ".kimi" / "config.toml")
        path.write_bytes(original.encode())
        agents.install("kimi")
        _toml_ok(path.read_text())
        agents.uninstall("kimi")
        assert path.read_bytes() == original.encode()


def test_kimi_array_of_arrays_is_not_mistaken_for_a_table(isolated_home):
    original = 'matrix = [\n  [1, 2],\n  [3, 4]\n]\nhooks = []\n'
    path = _kimi_cfg(isolated_home, original)
    agents.install("kimi")
    parsed = _toml_ok(path.read_text())
    if parsed:
        assert parsed["matrix"] == [[1, 2], [3, 4]] and len(parsed["hooks"]) == len(agents.KIMI_EVENTS)
    agents.uninstall("kimi")
    assert path.read_bytes() == original.encode()


@pytest.mark.parametrize("bad", ['{"hooks": []}', '{"hooks": {"BeforeTool": {"x": 1}}}'])
def test_unexpected_hooks_shapes_are_refused_not_rewritten(isolated_home, capsys, bad):
    path = isolated_home / "home" / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(bad)
    assert cli.main(["install-hooks", "--agent", "gemini"]) == 2
    assert "'hooks' must be an object" in capsys.readouterr().err
    assert path.read_text() == bad


def test_json_installer_keeps_a_utf8_bom(isolated_home):
    path = isolated_home / "home" / ".qwen" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + b'{"model": "x"}')
    agents.install("qwen")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf{")
    agents.uninstall("qwen")
    assert json.loads(path.read_bytes().decode("utf-8-sig"))["model"] == "x"


def test_codex_usage_hook_is_synchronous(isolated_home):
    """Overlapping async PostModelRequest hooks could append totals out of order (the UI keeps the last one)."""
    agents.install("codex")
    hooks = json.loads((isolated_home / "home" / ".codex" / "hooks.json").read_text())["hooks"]
    assert "async" not in hooks["PostModelRequest"][0]["hooks"][0]
    assert hooks["PreToolUse"][0]["hooks"][0]["async"] is True


def test_stale_pairing_lock_is_cleared_and_leaves_no_files(isolated_home):
    import os
    import time

    pairing.begin("lk", "T", {})
    lock = paths.home() / "state" / "lk.lock"
    lock.write_text("")
    old = time.time() - 60
    os.utime(lock, (old, old))  # a hook killed mid-update left this behind
    started = time.monotonic()
    pairing.begin("lk", "T", {})
    assert time.monotonic() - started < 1.5 and not lock.exists()
    assert sorted(p.name for p in (paths.home() / "state").iterdir()) == ["lk.json"]


def test_pairing_fallback_id_is_stable_and_distinct_per_call_shape():
    assert pairing.fallback_id("T", {"a": 1}) == pairing.fallback_id("T", {"a": 1})
    assert pairing.fallback_id("T", {"a": 1}) != pairing.fallback_id("T", {"a": 2})


def test_hook_survives_closed_stdout(monkeypatch):
    class Closed:
        def write(self, _):
            raise OSError("broken pipe")

        flush = write

    monkeypatch.setattr(sys, "stdout", Closed())
    assert agents.handle("codex", io.StringIO("{}")) == 0


def test_step_titles_for_claude_tools_without_an_explicit_case():
    assert cc.step_title("Skill", {"skill": "tdd"}) == "Skill"  # no recognised argument: just the name
    assert cc.step_title("LS", {"path": "/a/b"}) == "LS · b"  # these used to show only the tool name
    assert cc.step_title("mcp_like", {"query": "weather in nyc"}) == "mcp_like · weather in nyc"
