# Contributing

## Setup
```bash
git clone https://github.com/FugginOld/agentmon && cd agentmon
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Working on the UIs
```bash
# a frozen mid-run snapshot (matches the design mockups)
.venv/bin/agentmon demo --instant --until 55
# or a live replay at 4× speed in one terminal…
.venv/bin/agentmon demo --speed 4
# …and a UI in another
.venv/bin/agentmon web --open
.venv/bin/agentmon tui
```
Set `AGENTMON_HOME=/tmp/am` to keep test runs out of `~/.agentmon`.

## Rules of the road
- The core (`events`, `store`, `layout`, `tail`, `sink`, `paths`, `hooks`, `cli`) must stay
  **stdlib-only**, because the Claude Code hook imports it on every tool call.
- UI code renders only from `Run.snapshot()`. If a UI needs new data, add it to the
  snapshot (and a test) rather than reaching into `Run` directly.
- New event fields must be optional and documented in `docs/EVENTS.md`.
- The hook must never write to stdout or exit non-zero.

## Layout
```
src/agentmon/          core + CLI
src/agentmon/web/      FastAPI server + static frontend (no build step)
src/agentmon/tui/      Textual app + text renderers
tests/                 pytest (core, hook adapter, CLI, web, tui)
docs/                  EVENTS (schema), ARCHITECTURE, this file
examples/              emitting events from Python, shell and HTTP
```
