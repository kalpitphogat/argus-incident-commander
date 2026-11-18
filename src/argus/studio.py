"""Entry point for LangGraph Studio / LangGraph Platform (see langgraph.json).

The platform supplies its own checkpointer and store, so the graph is compiled
without them. Start a run with input like ``{"scenario_id": "checkout-deploy-regression"}``
and context like ``{"reasoning": "auto", "approval_policy": "medium"}``; resume the
approval interrupt with ``{"decision": "approve", "by": "you"}``.
"""

from argus.graph import build_graph

graph = build_graph()
