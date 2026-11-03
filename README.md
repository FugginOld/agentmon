# agentmon

**See what your AI agents are doing while they're doing it.** agentmon turns a stream of agent
events into a live workflow diagram: who spawned whom, which tool call is running right now,
what failed and what's waiting on it. It also tracks tokens, cost and context windows.

It ships two front ends over the same data. Install either one, or both:

| | Web UI | Terminal UI |
|---|---|---|
| Install | `pipx install "agentmon[web]"` | `pipx install "agentmon[tui]"` |
| Run | `agentmon web --open` | `agentmon tui` |
| Built with | FastAPI + Server-Sent Events + SVG (no build step, works offline) | Textual, box-drawing characters |
| Best for | A second monitor, sharing over SSH port-forward | Inside tmux/SSH, next to the agent |

Both get the same three views (**Swimlanes**, **DAG**, **Sequence**), plus a step inspector, a live event log,
per-agent context bars and a timeline.

```
╭ ✓ Write pkce.py ─────╮  ╭ ✗ Bash · pytest ─────╮
│ +96 new · 6.8s       ├─▶│ exit 1 · 2 failed    │
╰──────────────────────╯  ╰───────────┬──────────╯
                                      │ retry ×1
╭ ● Fix refresh exp ───╮              │
│ streaming · 00:14    │◀─────────────┘
╰───────────┬──────────╯
            ┊ after fix
            └┄┄┄┄┄┄┄┄┄┄┄┄▶ ╭ ○ Re-run pytest ┄┄┄┄┄╮
```

## Install

Python 3.10+. [pipx](https://pipx.pypa.io) is recommended because it keeps agentmon in its own
environment and puts `agentmon` on your PATH.

```bash
# from GitHub (until it's on PyPI)
pipx install "agentmon[web] @ git+https://github.com/FugginOld/agentmon"   # web dashboard
pipx install "agentmon[tui] @ git+https://github.com/FugginOld/agentmon"   # terminal UI
pipx install "agentmon[all] @ git+https://github.com/FugginOld/agentmon"   # both

# add the other UI later
pipx inject agentmon fastapi uvicorn      # → web
pipx inject agentmon textual              # → tui
```

The core (event capture, Claude Code hooks, `agentmon emit`) has **no dependencies**. Each extra
only adds the libraries that its UI needs.

## Try it in 10 seconds

```bash
agentmon demo --speed 2 &     # replays a scripted 4-agent run
agentmon tui                  # or: agentmon web --open
```

## Watch Claude Code

```bash
agentmon install-hooks            # adds hooks to ~/.claude/settings.json (backs it up first)
agentmon tui                      # then start a Claude Code session anywhere
```

Every Claude Code session becomes a run. Tool calls become steps, subagents get their own
lanes, prompts and replies appear in the sequence view, and token usage is read from the
session transcript. The hooks run asynchronously, always exit 0 and print nothing, so they never slow
Claude down or change what it does.

- `--scope project` writes `.claude/settings.json`; `--scope local` writes `.claude/settings.local.json`.
- `agentmon uninstall-hooks` removes only agentmon's entries.
- `AGENTMON_DISABLE=1` turns capture off for one session.

## Watch other agent CLIs

Codex, Qwen Code, Gemini CLI and Kimi CLI have hooks like Claude Code's. Pick one with `--agent`:

```bash
agentmon install-hooks --agent codex      # also: qwen, gemini, kimi
agentmon uninstall-hooks --agent codex
```

| `--agent` | Writes to | Scopes | Notes |
|---|---|---|---|
| `codex` | `~/.codex/hooks.json` | user, project | Codex skips new hooks until you approve them in `/hooks`. Token counts come from `PostModelRequest`. |
| `qwen` | `~/.qwen/settings.json` | user, project | Same events as Claude Code, so subagents and failures are captured. |
| `gemini` | `~/.gemini/settings.json` | user, project | Hooks are synchronous. No token counts (Gemini fires its model hook per streamed chunk). |
| `kimi` | `~/.kimi/config.toml` | user | Adds one `# >>> agentmon … # <<< agentmon` block of `[[hooks]]`; Kimi's hooks are Beta. A top-level `hooks = []` is commented out while installed (uninstall restores it); a non-empty one is refused. If Kimi rewrites its config the block may be dropped: re-run `install-hooks`. Tool calls made inside a subagent show on the main agent. |

These payloads are taken from each CLI's hooks reference, not from captured sessions. If a CLI changes
its fields, steps may show up missing or oddly titled; hook errors are logged to `~/.agentmon/hook-errors.log`.
On Windows, hook commands are written for a POSIX-style shell (Git Bash), as for Claude Code.

## Watch API calls (OpenAI, DeepSeek, Kimi, Qwen, Gemini...)

Wrap any OpenAI-compatible client. Every `chat.completions.create` call becomes a step with token usage:

```python
from openai import OpenAI
from agentmon.llm import watch

client = watch(OpenAI(base_url="https://api.deepseek.com", api_key=KEY), run="nightly", agent="coder", name="DeepSeek")
client.chat.completions.create(model="deepseek-chat", messages=[{"role": "user", "content": "hi"}])
```

| Provider | `base_url` |
|---|---|
| OpenAI | *(default)* |
| DeepSeek | `https://api.deepseek.com` |
| Kimi / Moonshot | `https://api.moonshot.ai/v1` |
| Qwen / DashScope | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` |

Calls return exactly what the SDK returned, errors are re-raised unchanged, and API keys and error text are never
written to the run file. Async clients are not monitored, and a streamed call only shows token counts if the
provider sends usage.

## Watch any other agent

Anything that can write a line of JSON can feed agentmon. See **[docs/EVENTS.md](docs/EVENTS.md)**.

```python
from agentmon import events, sink
sink.append(events.make("step_start", "my-run", step="s1", agent="planner", title="Plan", tool="Think"))
```
```bash
my-agent --jsonl | agentmon emit - --run nightly           # pipe JSONL events
agentmon emit step_end --run nightly --set step=s1         # one-off from a shell script
curl -X POST localhost:8765/api/events -H 'content-type: application/json' \
     -d '{"run":"r","type":"note","text":"hi"}'                # over HTTP (needs `agentmon web`)
```
There are runnable versions in [`examples/`](examples).

## Commands

| Command | |
|---|---|
| `agentmon web [--run ID] [--port 8765] [--open]` | Web dashboard (follows the newest run by default) |
| `agentmon tui [--run ID] [--view swim\|dag\|seq]` | Terminal UI |
| `agentmon install-hooks [--agent claude\|codex\|qwen\|gemini\|kimi] [--scope user\|project\|local] [--dry-run]` | Connect an agent CLI (default: Claude Code) |
| `agentmon uninstall-hooks [--agent …] [--scope …]` | Disconnect it |
| `agentmon emit TYPE --run ID [--set k=v] [--json '{…}']` / `emit -` | Append events |
| `agentmon demo [--speed N] [--instant] [--until SECONDS]` | Scripted sample run |
| `agentmon runs` | List recorded runs |

Runs are plain JSONL files in `~/.agentmon/runs/` (set `AGENTMON_HOME` to move them). Copy one
anywhere and open it with `agentmon tui --run path/to/file.jsonl`.

## Security

`agentmon web` binds to `127.0.0.1`. `POST /api/events` writes files, so only bind to another
interface (`--host 0.0.0.0`) on a network you trust. Run files can contain tool inputs and
outputs (file paths, commands, snippets of code), so treat `~/.agentmon` like your shell history.

## Docs

- [docs/EVENTS.md](docs/EVENTS.md): the event format
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it fits together, and the Claude Code mapping
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md): dev setup and project rules

MIT licensed.
