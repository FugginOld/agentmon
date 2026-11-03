"""Instrument any Python agent loop with agentmon events (no dependencies needed).

    python examples/python_emitter.py      # then: agentmon web  /  agentmon tui
"""

import random
import time
import uuid

from agentmon import events, sink

RUN = f"example-{uuid.uuid4().hex[:6]}"


def emit(etype: str, **fields) -> None:
    sink.append(events.make(etype, RUN, **fields))


emit("run_start", title="example research agent", source="examples/python_emitter.py")
emit("agent_start", agent="lead", name="Lead", model="opus")

emit("step_start", step="plan", agent="lead", title="Plan")
time.sleep(1)
emit("step_end", step="plan", summary="3 sub-questions", tokens={"in": 900, "out": 240})

emit("step_start", step="fanout", agent="lead", tool="Agent", title="Spawn researchers ×3")
workers = [f"r{i}" for i in range(3)]
for w in workers:
    emit("agent_start", agent=w, name=f"Researcher {w[-1]}", model="haiku", parent="lead", spawned_by="fanout")
emit("step_end", step="fanout", summary="3 researchers")

# a queued join step that depends on every worker's last step
emit("step_start", step="merge", agent="lead", title="Merge findings", status="queued",
     deps=[f"{w}-read" for w in workers])

for w in workers:
    emit("step_start", step=f"{w}-search", agent=w, tool="WebSearch", title="WebSearch · topic")
    time.sleep(random.uniform(0.3, 1.0))
    emit("step_end", step=f"{w}-search", summary=f"{random.randint(5, 12)} results")
    emit("step_start", step=f"{w}-read", agent=w, tool="WebFetch", title="WebFetch · source")
    time.sleep(random.uniform(0.5, 1.5))
    emit("step_end", step=f"{w}-read", summary="2.1k tok", tokens={"in": 2100})
    emit("agent_end", agent=w, status="done")

emit("step_start", step="merge", agent="lead")
time.sleep(1)
emit("step_end", step="merge", summary="report drafted", tokens={"in": 6400, "out": 1100})
emit("message", agent="lead", to="user", text="Report ready")
emit("run_end", status="done")
print(f"wrote run {RUN}")
