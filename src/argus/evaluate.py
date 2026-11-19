"""Chaos evaluation: root-cause accuracy and recovery rate on randomly generated faults.

Used by ``argus eval`` (and CI, with ``--strict``) and by the website build, which publishes the
numbers it measured instead of a hand-written claim.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict

FAMILY = {"bad_deploy": "Bad deploy", "pool_exhaustion": "Connection-pool exhaustion",
          "memory_leak": "Memory leak", "external_outage": "Third-party outage", "cert_expiry": "Expired certificate"}


async def run_one(seed: int, engine: str = "deterministic") -> dict:
    """Run one chaos incident end to end (approving every gate) and score it against the ground truth."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from .context import RunContext, forget_world
    from .graph import build_graph, new_incident_input
    from .sim import World, resolve

    spec = resolve("chaos", seed)
    app = build_graph(checkpointer=InMemorySaver())
    inp = new_incident_input(asdict(spec))
    cfg = {"configurable": {"thread_id": inp["incident_id"]}, "recursion_limit": 200}
    ctx = RunContext(reasoning=engine, approval_policy="never")
    t0 = time.perf_counter()
    payload: object = inp
    try:
        while True:
            await app.ainvoke(payload, cfg, context=ctx)
            st = await app.aget_state(cfg)
            if not st.interrupts:
                break
            payload = Command(resume={"decision": "approve", "by": "eval"})
    finally:
        forget_world(inp["incident_id"])
    values = st.values
    truth = World(resolve("chaos", seed)).fault.ground_truth()
    top = (values.get("hypotheses") or [{}])[0]
    return {"seed": seed, "fault": spec.fault, "kind": spec.fault["kind"],
            "rca": top.get("category") == truth.category and top.get("component") == truth.component,
            "fixed": values.get("status") in ("resolved", "mitigated"), "status": values.get("status"),
            "top": {"category": top.get("category"), "component": top.get("component")},
            "truth": {"category": truth.category, "component": truth.component},
            "rounds": values.get("round", 0), "secs": time.perf_counter() - t0}


async def evaluate(seeds: Iterable[int], engine: str = "deterministic",
                   on_result: Callable[[dict], None] | None = None) -> dict:
    rows = []
    for seed in seeds:
        row = await run_one(seed, engine)
        rows.append(row)
        if on_result:
            on_result(row)
    return summarize(rows, engine)


def summarize(rows: list[dict], engine: str = "deterministic") -> dict:
    by_kind: dict[str, Counter] = {}
    for r in rows:
        c = by_kind.setdefault(r["kind"], Counter())
        c["n"] += 1
        c["rca"] += r["rca"]
        c["fixed"] += r["fixed"]
    n = len(rows)
    return {
        "engine": engine, "n": n,
        "seeds": [rows[0]["seed"], rows[-1]["seed"]] if rows else [],
        "rca": sum(r["rca"] for r in rows), "fixed": sum(r["fixed"] for r in rows),
        "rounds_avg": round(statistics.mean(r["rounds"] for r in rows), 2) if rows else 0,
        "secs_median": round(statistics.median(r["secs"] for r in rows), 3) if rows else 0,
        "families": [{"kind": k, "label": FAMILY.get(k, k), "n": c["n"], "rca": c["rca"], "fixed": c["fixed"]}
                     for k, c in sorted(by_kind.items())],
        "misses": [{k: r[k] for k in ("seed", "fault", "top", "truth", "rca", "fixed")}
                   for r in rows if not (r["rca"] and r["fixed"])],
    }
