#!/usr/bin/env bash
# Send events over HTTP to a running `agentmon web` (default http://127.0.0.1:8765).
# Useful when the agent runs on another machine or in a container.
set -euo pipefail
URL="${AGENTMON_URL:-http://127.0.0.1:8765}/api/events"
RUN="curl-demo-$$"
# `agentmon web` requires its token (see ~/.agentmon/web-token). On another machine, copy it and set AGENTMON_TOKEN.
TOKEN="${AGENTMON_TOKEN:-$(cat "${AGENTMON_HOME:-$HOME/.agentmon}/web-token")}"

post() { curl -fsS -X POST "$URL" -H 'content-type: application/json' -H "Authorization: Bearer $TOKEN" -d "$1" >/dev/null; }

post "{\"run\":\"$RUN\",\"type\":\"run_start\",\"title\":\"curl demo\"}"
post "{\"run\":\"$RUN\",\"type\":\"agent_start\",\"agent\":\"bot\",\"name\":\"Bot\"}"
post "{\"run\":\"$RUN\",\"type\":\"step_start\",\"step\":\"s1\",\"agent\":\"bot\",\"tool\":\"Bash\",\"title\":\"Bash · make test\"}"
sleep 2
post "{\"run\":\"$RUN\",\"type\":\"step_end\",\"step\":\"s1\",\"summary\":\"exit 0\"}"
# a batch: a list of events in one request
post "[{\"run\":\"$RUN\",\"type\":\"message\",\"agent\":\"bot\",\"text\":\"all green\"},{\"run\":\"$RUN\",\"type\":\"run_end\",\"status\":\"done\"}]"
echo "sent run $RUN to $URL"
