"""HTTP API + web UI.

    GET  /                              mission-control UI
    GET  /api/health                    liveness + which reasoning engine is available
    GET  /api/scenarios                 incident scenarios you can simulate
    GET  /api/graph                     the compiled LangGraph (nodes/edges) for the live diagram
    GET  /api/topology                  the service map of the simulated estate
    POST /api/incidents                 start an incident run
    GET  /api/incidents                 list incidents
    GET  /api/incidents/{id}            state snapshot (works after a restart: read from the checkpointer)
    GET  /api/incidents/{id}/events     Server-Sent Events (replayable, Last-Event-ID aware)
    POST /api/incidents/{id}/decision   approve / edit / reject the pending remediation plan
    GET  /api/incidents/{id}/telemetry  metric series + change / alert / action markers for charts
    GET  /api/incidents/{id}/postmortem.md
    POST /api/webhooks/alertmanager     start incidents from Prometheus Alertmanager

Set ``ARGUS_API_TOKEN`` to require ``Authorization: Bearer <token>`` on every POST.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .brains import resolve_mode
from .brains.llm import model_label
from .context import DEFAULT_MODEL, get_world, world_ref
from .runner import IncidentManager
from .sim import CATALOG, BY_ID, Topology
from .tools import signals

log = logging.getLogger("argus.server")
WEB_DIR = Path(__file__).parent / "web"


class StartRequest(BaseModel):
    scenario: str = Field(description="Scenario id from /api/scenarios, or 'chaos'.")
    seed: int | None = None
    reasoning: Literal["auto", "llm", "deterministic"] = "auto"
    model: str | None = None
    approval_policy: Literal["low", "medium", "high", "never"] = "medium"
    pace: float = Field(default=0.0, ge=0, le=3)
    max_rounds: int = Field(default=3, ge=1, le=5)


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    approved_ids: list[str] | None = None
    edits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    feedback: str | None = None
    comment: str | None = None
    by: str = Field(default="on-call engineer", max_length=80)


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("ARGUS_API_TOKEN")
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(401, "missing or invalid bearer token")


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    manager = IncidentManager(Path(data_dir) if data_dir else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.startup()
        app.state.manager = manager
        yield
        await manager.shutdown()

    app = FastAPI(title="Argus - AI Incident Commander", version=__version__, lifespan=lifespan)

    def mgr() -> IncidentManager:
        return manager

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    async def health():
        mode = resolve_mode("auto", DEFAULT_MODEL)
        return {"ok": True, "version": __version__, "default_model": DEFAULT_MODEL,
                "model_label": model_label(DEFAULT_MODEL), "llm_available": mode == "llm",
                "auto_engine": model_label(DEFAULT_MODEL) if mode == "llm" else "Deterministic reasoner (offline)",
                "auth": bool(os.environ.get("ARGUS_API_TOKEN"))}

    @app.get("/api/scenarios")
    async def scenarios():
        return {"scenarios": [c.as_dict() for c in CATALOG]}

    @app.get("/api/graph")
    async def graph_shape():
        g = manager.graph.get_graph()
        inv = manager.graph.get_graph(xray=1)
        sub_nodes = [n for n in inv.nodes if n.startswith("investigate:")]
        return {
            "nodes": [{"id": n, "label": n.strip("_")} for n in g.nodes],
            "edges": [{"source": e.source, "target": e.target, "conditional": e.conditional} for e in g.edges],
            "investigator": [n.split(":", 1)[1] for n in sub_nodes],
        }

    @app.get("/api/topology")
    async def topology():
        return Topology().as_dict()

    # ------------------------------------------------------------- incidents
    @app.post("/api/incidents", dependencies=[Depends(require_token)])
    async def start(req: StartRequest, m: IncidentManager = Depends(mgr)):
        if req.scenario not in BY_ID and not req.scenario.startswith("chaos"):
            raise HTTPException(404, f"unknown scenario '{req.scenario}'")
        settings = req.model_dump(exclude={"scenario", "seed"})
        settings["model"] = req.model or DEFAULT_MODEL
        h = await m.create(req.scenario, req.seed, settings)
        return {"incident_id": h.id, "meta": h.meta}

    @app.get("/api/incidents")
    async def list_incidents(m: IncidentManager = Depends(mgr)):
        return {"incidents": await m.list()}

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str, m: IncidentManager = Depends(mgr)):
        snap = await m.snapshot(incident_id)
        if snap is None:
            raise HTTPException(404, "incident not found")
        return json.loads(json.dumps(snap, default=str))

    @app.get("/api/incidents/{incident_id}/events")
    async def events(incident_id: str, request: Request, after: int = Query(0, ge=0),
                     m: IncidentManager = Depends(mgr)):
        h = m.handles.get(incident_id) or await m._restore(incident_id)
        if h is None:
            raise HTTPException(404, "incident not found")
        last = request.headers.get("last-event-id")
        start_at = int(last) if last and last.isdigit() else after

        async def stream():
            yield "retry: 2000\n\n"
            async for ev in h.follow(start_at):
                if await request.is_disconnected():
                    break
                if ev is None:
                    yield ": keep-alive\n\n"
                    continue
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/incidents/{incident_id}/decision", dependencies=[Depends(require_token)])
    async def decide(incident_id: str, d: Decision, m: IncidentManager = Depends(mgr)):
        try:
            await m.decide(incident_id, d.model_dump())
        except KeyError:
            raise HTTPException(404, "incident not found") from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.get("/api/incidents/{incident_id}/telemetry")
    async def telemetry(incident_id: str, components: str = Query(..., description="comma separated"),
                        m: IncidentManager = Depends(mgr)):
        snap = await m.snapshot(incident_id)
        if snap is None:
            raise HTTPException(404, "incident not found")
        values = snap["values"]
        world = get_world({**world_ref(values), "incident_id": incident_id})
        out: dict[str, Any] = {"window_start": world.iso(0), "clock": world.clock, "now": world.iso(world.clock),
                               "components": {}}
        for name in [c for c in components.split(",") if c][:4]:
            if name not in world.topology:
                continue
            comp = world.topology.get(name)
            sig = signals(world, name, 60)
            metrics = {}
            for metric in comp.metrics:
                pts = world.series(name, metric, max(0, world.clock - 75))
                limit = None
                if metric == "latency_p99_ms":
                    limit = comp.slo_latency_ms
                elif metric == "error_rate_pct":
                    limit = comp.slo_error_pct
                elif metric == "connections":
                    limit = world.max_connections.get(name, comp.max_connections)
                entry = {"points": [[t, round(v, 3)] for t, v in pts], "limit": limit,
                         "anomaly": sig["metrics"][metric]["anomaly"]}
                if metric == "connections" and pts:
                    steps = [[t, world.cap_at(name, t)] for t, _ in pts]
                    if len({c for _, c in steps}) > 1:
                        entry["limit_points"] = steps
                metrics[metric] = entry
            out["components"][name] = {"kind": comp.kind, "health": world.health(name), "metrics": metrics}
        markers = [{"t": world.alert["fired_t"], "kind": "alert", "label": "Alert"}]
        relevant = set(out["components"])
        for c in world.changes:
            if c.t >= world.clock - 75 and (c.component in relevant or c.kind in ("feature_flag",) or
                                            c.author == "argus-agent"):
                markers.append({"t": c.t, "kind": "action" if c.author == "argus-agent" else c.kind,
                                "label": c.id, "text": c.summary})
        out["markers"] = markers
        out["health"] = {n: world.health(n) for n in world.topology.components}
        return out

    @app.get("/api/incidents/{incident_id}/postmortem.md", response_class=PlainTextResponse)
    async def postmortem(incident_id: str, m: IncidentManager = Depends(mgr)):
        snap = await m.snapshot(incident_id)
        if snap is None or not (snap["values"].get("report") or {}).get("postmortem"):
            raise HTTPException(404, "postmortem not ready")
        return PlainTextResponse(snap["values"]["report"]["postmortem"], media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{incident_id}-postmortem.md"'})

    # -------------------------------------------------------------- webhooks
    @app.post("/api/webhooks/alertmanager", dependencies=[Depends(require_token)])
    async def alertmanager(payload: dict, m: IncidentManager = Depends(mgr)):
        """Prometheus Alertmanager webhook -> one incident per firing alert.

        With the simulator backend the alert's `argus_scenario` label (or its `service` label) selects
        which simulated estate to investigate; a production backend would query real telemetry instead.
        """
        started = []
        for alert in payload.get("alerts", []):
            if alert.get("status", "firing") != "firing":
                continue
            labels = alert.get("labels", {})
            scenario = labels.get("argus_scenario")
            if not scenario:
                svc = labels.get("service")
                scenario = next((c.id for c in CATALOG if svc and (c.fault.get("service") == svc or
                                                                  c.fault.get("via") == svc)), "chaos")
            h = await m.create(scenario, None, {"approval_policy": labels.get("argus_policy", "medium")})
            started.append(h.id)
        return {"started": started}

    # --------------------------------------------------------------------- UI
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def app_factory() -> FastAPI:  # uvicorn --factory argus.server:app_factory
    return create_app(os.environ.get("ARGUS_DATA_DIR", ".argus"))
