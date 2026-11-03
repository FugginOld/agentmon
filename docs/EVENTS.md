# agentmon event format (v1)

agentmon watches **runs**. A run is an append-only stream of JSON events, one per line,
stored at `~/.agentmon/runs/<run-id>.jsonl` (override the folder with `AGENTMON_HOME`).
Both UIs tail these files, so anything that can append a line of JSON can feed the monitor.

There are three ways to send events:

| How | Use when | Example |
|---|---|---|
| Append to the run file | Your agent runs on the same machine | `examples/python_emitter.py` |
| `agentmon emit` | Shell scripts, or piping JSONL from another process | `my-agent \| agentmon emit - --run job-42` |
| `POST /api/events` | Agents on another host (requires `agentmon web`; send `Authorization: Bearer <contents of ~/.agentmon/web-token>`) | `examples/curl_events.sh` |

## Common fields

| Field | Required | Meaning |
|---|---|---|
| `v` | no (defaults to 1) | Schema version |
| `ts` | no (defaults to now) | ISO-8601 string (`2026-11-03T01:14:10.123Z`) or epoch seconds |
| `run` | **yes** | Run id. Also names the file. |
| `type` | **yes** | One of the types below |

Unknown extra fields are kept in the file and ignored by the reducer, so you can add your own.

Numbers must be finite: `NaN`, `Infinity` and `-Infinity` (which some JSON encoders emit) are rejected, as is a
boolean `ts`. `POST /api/events` answers `400` with `{"error": …, "code": …}`, where `code` is one of
`not_object`, `unknown_type`, `bad_run`, `missing_field`, `bad_ts`, `bad_status`, `bad_deps`, `non_finite`.
The reply never echoes what you sent. New run files are created owner-only (`0600`, in a `0700` folder) on POSIX.

## Event types

### `run_start` / `run_end`
```json
{"type":"run_start","run":"job-42","title":"Nightly refactor","cwd":"/repo","source":"my-framework"}
{"type":"run_end","run":"job-42","status":"done"}
```
`status`: `done`, `error`, `stopped` (any string is accepted; `error` is shown in red).
A run with no events for 10 minutes and no `run_end` is shown as **stale**.

### `agent_start` / `agent_end`
```json
{"type":"agent_start","run":"job-42","agent":"coder","name":"Coder","model":"sonnet","parent":"lead","spawned_by":"s5"}
{"type":"agent_end","run":"job-42","agent":"coder","status":"done","summary":"PKCE implemented"}
```
- `agent` (required): stable id. Each agent gets a lane and a colour.
- `parent`: the agent that spawned it. This draws a spawn message in the sequence view.
- `spawned_by`: the step that spawned it. If you leave it out but the parent has an open
  `Task`/`Agent` tool step, agentmon links them automatically.
- `kind`: free-form agent type (for example `explorer` or `reviewer`). It is used for that automatic matching.

### `step_start` / `step_end`
A step is one node in the graph: a tool call, a reasoning phase, or a planned task.
```json
{"type":"step_start","run":"job-42","step":"c3","agent":"coder","title":"Fix refresh expiry","tool":"Edit",
 "input":{"file_path":"auth/pkce.py"},"deps":["t2"]}
{"type":"step_end","run":"job-42","step":"c3","status":"done","summary":"+3 −1","output":"…","tokens":{"in":1200,"out":80}}
```
- `step` (required): unique within the run. `agent` is required on `step_start`.
- `deps`: ids of steps this one depends on. These are the graph edges. If you omit `deps`, the
  step depends on the previous step of the same agent. An agent's first step depends on the step that spawned the agent.
- `status` on `step_start`: `running` (default) or `queued`. A queued step shows as a dashed
  placeholder. Send `step_start` again with the same id (no status) when it begins.
- `status` on `step_end`: `done` (default) or `error`.
- Steps with a `tool` also appear as call and return arrows to **tools** in the sequence view.
- Ordering is forgiving. A `step_end` that arrives before its `step_start` is fine, and a
  finished step is never moved back to running.

### `message`
```json
{"type":"message","run":"job-42","agent":"tester","to":"lead","text":"tests failed ×2","kind":"error"}
```
`agent` is the sender. `to` is an agent id or `user` (the default). `kind`: `message` or `error`.

### `thinking`
```json
{"type":"thinking","run":"job-42","agent":"coder","text":"Both failures trace to…"}
```
The latest thought of the agent that owns the running step is shown in the inspector.

### `usage`
Absolute running totals for one agent. Each event replaces the previous totals; it doesn't add to them.
```json
{"type":"usage","run":"job-42","agent":"coder","model":"sonnet",
 "tokens":{"in":24800,"out":2900,"cache_read":19100,"cache_write":0},
 "context":{"used":41200,"max":200000},"cost":0.19}
```
If an agent never sends `usage`, its token count is the sum of its steps' `tokens`.

### `note`
```json
{"type":"note","run":"job-42","text":"rate limited, backing off","level":"warn"}
```
`level`: `info`, `warn` or `error` (error notes count toward the error total).
