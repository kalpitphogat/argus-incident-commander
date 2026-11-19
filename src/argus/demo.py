"""Export a static, serverless demo of Argus (e.g. for GitHub Pages).

Every incident in the demo is a *recording of the real agent*: the exporter runs
the actual LangGraph through the same runner the web server uses (offline
reasoner, approval policy "medium"), captures every UI event, and saves the
telemetry the charts need. Two branches are recorded per incident: approve the
first remediation plan, or reject it with feedback (Argus re-plans). Before each
curated scenario a "previous occurrence" is resolved first, so the recorded run
genuinely recalls it from incident memory.

The web UI detects demo mode (a ``<meta name="argus-demo">`` tag) and replays
the recordings in the browser with ``web/demo.js``. The site around it (the landing
page at the root, see ``sitegen.py``) is built from the same recordings plus a chaos
benchmark that runs during the export.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.store.memory import InMemoryStore

from . import __version__
from .brains.llm import model_label
from .context import DEFAULT_MODEL, forget_world, get_world
from .graph import build_graph
from .runner import IncidentManager
from .sim import CATALOG, Topology, chaos_fault
from .sim.world import World
from .tools import signals

WEB_DIR = Path(__file__).parent / "web"
SETTINGS = {"reasoning": "deterministic", "approval_policy": "medium", "pace": 0.0}
REJECT_FEEDBACK = "Not this one - it's too risky right now. Find a lower-risk option first."
DECISION = {"approved_ids": None, "edits": {}, "comment": None}
BENCH_START = 1000  # chaos seeds for the published benchmark (CI evaluates 500-549)


def _iid(*parts: object) -> str:
    return "INC-" + hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:6].upper()


async def _record(scenario: str, seed: int | None, incident_id: str, created_at: datetime, store,
                  reject_first: bool) -> dict:
    mgr = IncidentManager(None, store=store)
    await mgr.startup()
    try:
        h = await mgr.create(scenario, seed, dict(SETTINGS), incident_id=incident_id, created_at=created_at)
        world = get_world({"incident_id": h.id})
        original_push = h.push

        async def push(event: dict) -> None:  # stamp each event with the simulated clock
            await original_push({**event, "_clock": world.clock})

        h.push = push
        decisions = 0
        while True:
            await h.task
            if h.status != "awaiting_approval":
                break
            decisions += 1
            if decisions > 6:
                raise RuntimeError(f"{scenario}: too many approval rounds")
            if reject_first and decisions == 1:
                d = {**DECISION, "decision": "reject", "feedback": REJECT_FEEDBACK, "by": "you"}
            else:
                d = {**DECISION, "decision": "approve", "feedback": None, "by": "you"}
            await mgr.decide(h.id, d)
        values = (await mgr.graph.aget_state(mgr._cfg(h.id))).values
        events = [dict(e) for e in h.events]
        for e in events:
            e.setdefault("_clock", world.start_clock)
        return {"status": h.status, "events": events, "telemetry": _telemetry(world, events),
                "postmortem": (values.get("report") or {}).get("postmortem"), "decisions": decisions}
    finally:
        forget_world(incident_id)
        await mgr.shutdown()


def _telemetry(world: World, events: list[dict]) -> dict:
    """Everything the charts / service map need, for any moment of the recording."""
    comps = [world.alert["service"]]
    for e in events:
        if e["type"] == "hypotheses":
            comps += [h["component"] for h in e["hypotheses"]]
        elif e["type"] == "finding":
            comps.append(e["finding"]["target"])
    comps = [c for c in dict.fromkeys(comps) if c in world.topology]
    end = world.clock
    series, limits, anomaly, caps = {}, {}, {}, {}
    for name in comps:
        comp = world.topology.get(name)
        sig = signals(world, name, 60)
        series[name] = {m: [round(v, 2) for _, v in world.series(name, m, 0, end)] for m in comp.metrics}
        limits[name] = {"latency_p99_ms": comp.slo_latency_ms, "error_rate_pct": comp.slo_error_pct}
        anomaly[name] = {m: (sig["metrics"][m]["anomaly"] or {}).get("onset_t") for m in comp.metrics}
        if comp.max_connections:
            caps[name] = [[0, comp.max_connections]] + [[at, v] for at, v in world._cap_history.get(name, [])]
    clocks = sorted({e["_clock"] for e in events})
    health = {str(c): {n: world.health_at(n, c) for n in world.topology.components} for c in clocks}
    changes = [{"id": c.id, "t": c.t, "kind": c.kind, "component": c.component, "summary": c.summary,
                "author": c.author} for c in world.changes]
    return {"anchor": world.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"), "window_start": world.iso(0),
            "alert_t": world.alert["fired_t"], "end": end,
            "kinds": {n: world.topology.get(n).kind for n in comps}, "series": series, "limits": limits,
            "anomaly": anomaly, "caps": caps, "health": health, "changes": changes}


def _first_interrupt(events: list[dict]) -> dict | None:
    return next((e["interrupt"] for e in events if e["type"] == "approval_required"), None)


async def export_incident(scenario: str, seed: int | None, created_at: datetime, warmup: bool) -> dict:
    """Record both branches (approve / reject-with-feedback) of one incident."""
    iid = _iid(scenario, seed)
    prev_id = _iid("previous", scenario, seed)

    async def fresh_store() -> InMemoryStore:
        store = InMemoryStore()
        if warmup:  # a real earlier occurrence, so the recorded run recalls it from memory
            await _record(scenario, (seed or 0) + 1000, prev_id, created_at - timedelta(days=9), store, False)
        return store

    approve = await _record(scenario, seed, iid, created_at, await fresh_store(), reject_first=False)
    out = {"incident_id": iid, "scenario": scenario, "seed": seed, "approve": approve}
    first = _first_interrupt(approve["events"])
    if first:
        reject = await _record(scenario, seed, iid, created_at, await fresh_store(), reject_first=True)
        if _first_interrupt(reject["events"]) == first:  # identical up to the decision
            cut = next(i for i, e in enumerate(reject["events"]) if e["type"] == "decision")
            reject["events"] = reject["events"][cut:]
            out["reject"] = reject
    return out


def _pick_chaos(n: int) -> list[int]:
    """Seeds for `n` chaos incidents: different fault families first, never a curated scenario's target."""
    curated = {(c.fault["kind"], c.fault.get("service") or c.fault.get("datastore") or c.fault.get("provider"))
               for c in CATALOG}
    kinds, targets, seeds = set(), set(), []
    for distinct_kinds in (True, False):
        for seed in range(1, 500):
            if len(seeds) >= n:
                return seeds
            f = chaos_fault(seed)
            key = (f["kind"], f.get("service") or f.get("datastore") or f.get("provider"))
            if key in curated or key in targets or (distinct_kinds and f["kind"] in kinds):
                continue
            kinds.add(f["kind"])
            targets.add(key)
            seeds.append(seed)
    return seeds


def _graph_shape() -> dict:
    app = build_graph()
    g = app.get_graph()
    inv = app.get_graph(xray=1)
    return {"nodes": [{"id": n, "label": n.strip("_")} for n in g.nodes],
            "edges": [{"source": e.source, "target": e.target, "conditional": e.conditional} for e in g.edges],
            "investigator": [n.split(":", 1)[1] for n in inv.nodes if n.startswith("investigate:")]}


async def export_site(out: Path, repo_url: str | None = None, chaos: int = 4, log=print, bench: int = 100) -> dict:
    """Build the whole website: the landing page at ``out/`` and the replayable demo app at ``out/demo/``."""
    from .evaluate import evaluate
    from .sitegen import build_landing

    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    demo = out / "demo"
    (demo / "data" / "scenarios").mkdir(parents=True)
    (demo / "data" / "chaos").mkdir(parents=True)
    shutil.copytree(WEB_DIR, demo / "static")
    index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    tags = '<meta name="argus-demo" content="data/">\n  <meta name="argus-home" content="../">'
    if repo_url:
        tags += f'\n  <meta name="argus-repo" content="{repo_url}">'
    (demo / "index.html").write_text(index.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  {tags}', 1),
                                     encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    created = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
    sizes, recordings = {}, {}
    for card in CATALOG:
        data = await export_incident(card.id, card.seed, created, warmup=True)
        recordings[card.id] = data
        path = demo / "data" / "scenarios" / f"{card.id}.json"
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        sizes[card.id] = path.stat().st_size
        log(f"  {card.id:32s} approve={data['approve']['status']:9s} "
            f"reject={(data.get('reject') or {}).get('status', '-'):9s} {sizes[card.id] / 1024:,.0f} KB")
    seeds = _pick_chaos(chaos)
    for seed in seeds:
        data = await export_incident("chaos", seed, created, warmup=False)
        path = demo / "data" / "chaos" / f"{seed}.json"
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        sizes[f"chaos-{seed}"] = path.stat().st_size
        log(f"  chaos-{seed:<26d} {chaos_fault(seed)['kind']:16s} approve={data['approve']['status']}")

    meta = {
        "version": __version__, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_url": repo_url, "chaos": seeds,
        "health": {"ok": True, "version": __version__, "default_model": DEFAULT_MODEL,
                   "model_label": model_label(DEFAULT_MODEL), "llm_available": False,
                   "auto_engine": "Deterministic reasoner (offline)", "auth": False, "demo": True},
        "scenarios": [c.as_dict() for c in CATALOG],
        "graph": _graph_shape(),
        "topology": Topology().as_dict(),
    }

    stats = None
    if bench > 0:  # measured now, published on the landing page
        stats = await evaluate(range(BENCH_START, BENCH_START + bench))
        log(f"  benchmark: {stats['rca']}/{stats['n']} root causes correct, {stats['fixed']}/{stats['n']} recovered "
            f"(seeds {BENCH_START}-{BENCH_START + bench - 1})")
        meta["benchmark"] = stats
    (demo / "data" / "meta.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    landing = build_landing(out, meta=meta, cards=CATALOG, recordings=recordings, bench=stats, repo_url=repo_url)
    log(f"  landing page: {landing['reels']} replay reels, {landing['bytes'] / 1024:,.0f} KB of HTML")
    return {"incidents": len(sizes), "bytes": sum(sizes.values()), "benchmark": stats, "landing": landing}


def main(out: str, repo_url: str | None, chaos: int, bench: int = 100) -> None:
    summary = asyncio.run(export_site(Path(out), repo_url, chaos, bench=bench))
    print(f"Exported {summary['incidents']} recorded incidents ({summary['bytes'] / 1024:,.0f} KB) to {out}")
