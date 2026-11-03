#!/usr/bin/env bash
# Instrument a shell pipeline with `agentmon emit` (no server needed).
set -euo pipefail
RUN="deploy-$(date +%s)"
am() { agentmon emit "$@" --run "$RUN"; }

am run_start --set title='"nightly deploy"'
am agent_start --set agent=ci --set name=CI
for stage in build test deploy; do
  am step_start --set step="$stage" --set agent=ci --set tool=Bash --set title="\"Bash · $stage\""
  if sleep 1; then am step_end --set step="$stage" --set summary='"exit 0"'
  else am step_end --set step="$stage" --set status=error; fi
done
am run_end --set status=done
echo "run $RUN recorded — view it with: agentmon tui --run $RUN"
