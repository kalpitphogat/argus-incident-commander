import os
from dataclasses import asdict
from datetime import datetime, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

# Never let a developer's real key leak into the offline test-suite.
for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "ARGUS_API_TOKEN"):
    os.environ.pop(var, None)

from argus.context import RunContext, forget_world  # noqa: E402
from argus.graph import build_graph, new_incident_input  # noqa: E402

ANCHOR = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)


async def run_incident(spec, decisions=None, policy="medium", reasoning="deterministic", store=None,
                       checkpointer=None, collect=None):
    """Run a full incident; `decisions` answers each approval interrupt in order (default: approve)."""
    app = build_graph(checkpointer=checkpointer or InMemorySaver(), store=store or InMemoryStore())
    inp = new_incident_input(asdict(spec), created_at=ANCHOR)
    ctx = RunContext(reasoning=reasoning, approval_policy=policy)
    inp["settings"] = asdict(ctx)
    cfg = {"configurable": {"thread_id": inp["incident_id"]}, "recursion_limit": 200}
    decisions = list(decisions or [])
    interrupts = []
    payload = inp
    try:
        while True:
            async for _ns, _mode, ev in app.astream(payload, cfg, context=ctx, stream_mode=["custom"], subgraphs=True):
                if collect is not None:
                    collect.append(ev)
            st = await app.aget_state(cfg)
            if not st.interrupts:
                return st.values, interrupts
            interrupts.append(st.interrupts[0].value)
            payload = Command(resume=decisions.pop(0) if decisions else {"decision": "approve", "by": "pytest"})
    finally:
        forget_world(inp["incident_id"])


@pytest.fixture
def run():
    return run_incident
