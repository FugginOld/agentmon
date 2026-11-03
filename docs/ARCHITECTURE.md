# Architecture

```
 producers                     storage                         consumers
 ─────────                     ───────                         ─────────
 Claude Code hooks ─┐
 agentmon emit ─────┼──► ~/.agentmon/runs/<run>.jsonl ──► RunWatcher ──► Run (reducer) ──► snapshot()
 POST /api/events ──┘        (append-only JSONL)          (tail.py)      (store.py)          │
 your agent ────────┘                                                                         ├─► web UI  (FastAPI + SSE + SVG)
                                                                                              └─► TUI     (Textual + box drawing)
```

## Design choices

- **Files are the bus.** Producers only append lines, and consumers only tail files. No daemon has to
  be running for events to be captured, several UIs can watch the same run, and a run file
  is a complete, replayable trace. You can `cp` it, attach it to a bug report, or open it later.
- **One reducer, two front ends.** `store.Run.apply()` is the only place state changes.
  `Run.snapshot()` returns plain JSON-able data, and both UIs render only from it, so they can't disagree.
- **Grid layouts are shared.** `layout.py` assigns swimlane rows, DAG levels and columns, the critical
  path, sequence rows and timeline bars. The web UI scales the grid to pixels; the TUI scales it to
  character cells. Each UI routes edges orthogonally through gap rows and lane gutters.
- **The core has zero dependencies.** `agentmon hook` runs on every Claude Code tool call, so its
  import path (`cli → hooks.claude_code → events/sink/paths`) is pure stdlib. FastAPI and Textual
  are only imported by `agentmon web` and `agentmon tui`, which ship as the `web` and `tui` extras.
- **The hooks never interfere.** The hook always exits 0 and writes nothing to stdout, so Claude Code
  treats it as a no-op. Tool and subagent hooks are registered `async`, so they never delay
  Claude. Failures go to `~/.agentmon/hook-errors.log`.
- **The reducer is forgiving.** Async hooks can deliver `PostToolUse` before `PreToolUse`. The reducer
  creates the step from whichever event arrives first and never moves a finished step back to running.

## Modules

| Module | Role |
|---|---|
| `events.py` | Schema v1: types, required fields, `make()`, `validate()`, `parse_line()` |
| `sink.py` | Locked append of one event line to a run file |
| `tail.py` | `RunWatcher` (incremental tail that handles partial lines and truncation), `list_runs`, `resolve` |
| `store.py` | `Run` reducer, metrics, focus step, `snapshot()` |
| `layout.py` | Swimlanes, DAG plus critical path, sequence, timeline |
| `hooks/claude_code.py` | Hook payload → events, transcript token usage, `install`/`uninstall` for any JSON-settings CLI (`Target`); also serves Codex and Qwen Code, which send the same payloads |
| `hooks/generic.py` | Gemini CLI and Kimi CLI payload → events (tool calls paired through `pairing`) |
| `hooks/pairing.py` | Per-run state in `~/.agentmon/state/`: Pre/Post pairing without a tool-call id, Codex token totals |
| `hooks/agents.py` | Registry: per-CLI translate, stdout rule, install targets, Kimi's TOML block |
| `auth.py`, `web/auth.py` | The web token (`~/.agentmon/web-token`, owner-only) and the ASGI middleware that requires it on every route |
| `llm.py` | `watch(client)`: instruments `chat.completions.create` of any OpenAI-compatible client |
| `demo.py` | Scripted four-agent run used by `agentmon demo` and the tests |
| `web/` | FastAPI server (`/api/stream` SSE, `/api/events` ingest) and static vanilla-JS frontend |
| `tui/` | Textual app plus pure `render.py` text renderers |

## Claude Code mapping

| Hook | Events emitted |
|---|---|
| `SessionStart` | `run_start` (run id = `session_id`, title = repo folder) + `agent_start main` |
| `UserPromptSubmit` | `message user→main` + an instant "Prompt" step |
| `PreToolUse` | `step_start` (step id = `tool_use_id`, agent = `agent_id` or `main`) |
| `PostToolUse` / `PostToolUseFailure` | `step_end` done / error |
| `SubagentStart` / `SubagentStop` | `agent_start` / `usage` (from the subagent transcript) + `agent_end` |
| `Stop` | `usage` for main (from the transcript) + `message main→user` |
| `Notification`, `PreCompact` | `note` |
| `SessionEnd` | `run_end` |

Token totals are read best-effort from the transcript JSONL (`message.usage`, deduplicated by message id).
That format isn't formally documented by Claude Code, so a missing or changed field just means no
token numbers. Nothing breaks.
