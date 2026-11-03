"""Registry of the agent CLIs agentmon can hook into: one place for translate, stdout rule and installer."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import events
from . import claude_code as cc
from . import generic, pairing

AGENTS = ("claude", "codex", "qwen", "gemini", "kimi")
JSON_STDOUT = frozenset({"codex", "gemini"})  # these CLIs parse hook stdout as JSON, so answer "{}"
SCOPES = {
    "claude": ("user", "project", "local"),
    "codex": ("user", "project"),
    "qwen": ("user", "project"),
    "gemini": ("user", "project"),
    "kimi": ("user",),
}
DISPLAY = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code", "gemini": "Gemini CLI", "kimi": "Kimi CLI"}

CODEX_EVENTS = {"SessionStart": False, "UserPromptSubmit": True, "PreToolUse": True, "PostToolUse": True,
                "SubagentStart": True, "SubagentStop": True, "PreCompact": True, "PostModelRequest": False,
                "Stop": True, "SessionEnd": False}
GEMINI_EVENTS = {e: False for e in ("SessionStart", "BeforeAgent", "BeforeTool", "AfterTool", "AfterAgent",
                                    "Notification", "PreCompress", "SessionEnd")}
KIMI_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "SubagentStart", "SubagentStop", "StopFailure", "Notification", "PreCompact", "PostCompact",
               "SessionEnd")

TARGETS = {
    "claude": cc.CLAUDE,
    "codex": cc.Target(events=CODEX_EVENTS, agent="codex", dirname=".codex", filename="hooks.json",
                       local_filename=None, matcher=None, timeouts={"SessionEnd": 3}),
    "qwen": cc.Target(events=cc.HOOK_EVENTS, agent="qwen", dirname=".qwen", filename="settings.json",
                      local_filename=None, matcher=None),
    # Gemini's hook timeout is in milliseconds.
    "gemini": cc.Target(events=GEMINI_EVENTS, agent="gemini", dirname=".gemini", filename="settings.json",
                        local_filename=None, matcher=None, timeout=10000),
}


# --------------------------------------------------------------------------- translate
def _tokens(usage: dict) -> dict[str, int]:
    def pick(*names: str) -> int:
        for n in names:
            if isinstance(usage.get(n), (int, float)):
                return int(usage[n])
        return 0

    return {"in": pick("input_tokens", "prompt_tokens"), "out": pick("output_tokens", "completion_tokens"),
            "cache_read": pick("cached_input_tokens", "cached_tokens", "cache_read_input_tokens")}


def _codex(payload: dict, now: float | None = None) -> list[dict]:
    out = cc.translate(payload, now, label="codex", agent_name="Codex")
    run, usage = payload.get("session_id"), payload.get("token_usage")
    if payload.get("hook_event_name") == "PostModelRequest" and run and isinstance(usage, dict):
        agent = payload.get("agent_id") or cc.MAIN
        # A failing state file raises into run_hook, which logs it and drops only this usage-only payload.
        totals = pairing.add_tokens(run, agent, _tokens(usage))
        out.append(events.make("usage", run, ts=time.time() if now is None else now, agent=agent, tokens=totals))
    return out


def translate(agent: str, payload: dict, now: float | None = None) -> list[dict]:
    if agent == "codex":
        return _codex(payload, now)
    if agent == "qwen":
        return cc.translate(payload, now, label="qwen-code", agent_name="Qwen")
    if agent in generic.FLAVORS:
        return generic.translate(payload, agent, now)
    return cc.translate(payload, now)


def handle(agent: str = "claude", stream=None) -> int:
    """Entry point for ``agentmon hook --agent X``."""
    return cc.run_hook(lambda payload: translate(agent, payload), stream, json_stdout=agent in JSON_STDOUT)


# --------------------------------------------------------------------------- kimi (TOML)
_BEGIN, _END = "# >>> agentmon", "# <<< agentmon"
_BLOCK = re.compile(rf"\r?\n?{re.escape(_BEGIN)}.*?{re.escape(_END)}[^\S\r\n]*(?:\r?\n)?", re.S)


def kimi_path() -> Path:
    return Path.home() / ".kimi" / "config.toml"


def kimi_block(nl: str = "\n") -> str:
    command = json.dumps(cc.hook_command("kimi"))  # a JSON string is a valid TOML basic string
    hooks = [f'[[hooks]]{nl}event = "{event}"{nl}command = {command}{nl}timeout = 10' for event in KIMI_EVENTS]
    return nl.join([_BEGIN + " (managed by `agentmon install-hooks --agent kimi`)", *hooks, _END]) + nl


def _kimi_strip(text: str) -> tuple[str, int]:
    removed = sum(m.group(0).count("[[hooks]]") for m in _BLOCK.finditer(text))
    return _BLOCK.sub("", text), removed


# A config Kimi saved itself has `hooks = []` at the top level; `[[hooks]]` tables after that are invalid TOML.
_OFF = "# agentmon-off: "
_EMPTY = r"[ \t]*hooks[ \t]*=[ \t]*\[[ \t]*\][ \t]*(?:#[^\r\n]*)?"  # `hooks = []`, maybe indented or commented
_EMPTY_HOOKS = re.compile(rf"^{_EMPTY}(?=\r?$)", re.M)
_RESTORE = re.compile(rf"^{re.escape(_OFF)}({_EMPTY})(?=\r?$)", re.M)
_ANY_HOOKS_KEY = re.compile(r"^[ \t]*hooks[ \t]*=", re.M)
_HOOKS_TABLE = re.compile(r"^[ \t]*\[\[[ \t]*hooks[ \t]*\]\]", re.M)
_KEY_CHARS = "[A-Za-z0-9_. \"'-]"
# A real table header ([name] / [[name]]), not a `[1, 2]` line inside a multi-line array.
_FIRST_TABLE = re.compile(rf"^[ \t]*\[\[?[ \t]*{_KEY_CHARS}+?[ \t]*\]\]?[ \t]*(?:#[^\r\n]*)?(?=\r?$)", re.M)


def _park_empty_hooks(text: str, path: Path) -> str:
    """Comment out a top-level ``hooks = []`` (uninstall restores it); refuse a non-empty top-level array."""
    m = _FIRST_TABLE.search(text)
    split = m.start() if m else len(text)
    head, rest = text[:split], text[split:]
    if _EMPTY_HOOKS.search(head):
        head = _EMPTY_HOOKS.sub(lambda m: _OFF + m.group(0), head, count=1)
    elif _ANY_HOOKS_KEY.search(head):
        raise ValueError(f"{path} already sets hooks = [...] at the top level; convert them to [[hooks]] tables first")
    return head + rest


def kimi_install(dry_run: bool = False) -> tuple[Path, str]:
    path = kimi_path()
    text = cc.read_text_exact(path) if path.exists() else ""
    nl = "\r\n" if "\r\n" in text else "\n"
    text, _ = _kimi_strip(text)
    text = _park_empty_hooks(text, path)
    block = kimi_block(nl)
    # A blank line before the block; _BLOCK swallows exactly that newline on uninstall.
    new = text + ("" if not text else nl if text.endswith("\n") else nl + nl) + block
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            cc.write_backup(path)
        cc.write_text_exact(path, new)
    return path, block


def kimi_uninstall(dry_run: bool = False) -> tuple[Path, int]:
    path = kimi_path()
    if not path.exists():
        return path, 0
    old = cc.read_text_exact(path)
    new, removed = _kimi_strip(old)
    if not _HOOKS_TABLE.search(new):  # tables of the user's own would clash with a restored `hooks = []`
        new = _RESTORE.sub(lambda m: m.group(1), new, count=1)
    if new != old and not dry_run:
        cc.write_text_exact(path, new)
    return path, removed


# --------------------------------------------------------------------------- install dispatch
def install(agent: str, scope: str = "user", dry_run: bool = False) -> tuple[Path, str, int]:
    """Register agentmon's hooks with one CLI. Returns (config path, preview text, number of hooked events)."""
    if agent == "kimi":
        path, block = kimi_install(dry_run)
        return path, block, len(KIMI_EVENTS)
    target = TARGETS[agent]
    path, settings = cc.install(scope, dry_run, target=target)
    return path, json.dumps({"hooks": settings.get("hooks", {})}, indent=2), len(target.events)


def uninstall(agent: str, scope: str = "user", dry_run: bool = False) -> tuple[Path, int]:
    if agent == "kimi":
        return kimi_uninstall(dry_run)
    return cc.uninstall(scope, dry_run, target=TARGETS[agent])
