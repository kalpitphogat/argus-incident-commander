"""Runs incidents in the background and fans their events out to any number of viewers.

A run is decoupled from HTTP connections: the graph keeps going if the browser
disconnects, every event is kept in an ordered log (so a client can reconnect
with ``Last-Event-ID`` and replay), and a run paused on ``interrupt()`` waits
in the checkpointer -- for minutes or days -- until someone decides.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from .brains import resolve_mode
from .brains.llm import model_label
from .context import RunContext, forget_world, register_world
from .graph import build_graph, new_incident_input
from .sim import resolve
from .sim.world import World

log = logging.getLogger("argus.runner")
RUNS_NS = ("argus", "runs")
FINAL = frozenset({"resolved", "mitigated", "escalated", "failed"})
SETTINGS_KEYS = ("reasoning", "model", "approval_policy", "max_rounds", "max_attempts", "max_tool_calls", "pace")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class IncidentHandle:
    def __init__(self, incident_id: str, meta: dict) -> None:
        self.id = incident_id
        self.meta = meta
        self.events: list[dict] = []
        self.status = "starting"
        self.interrupt: dict | None = None
        self.task: asyncio.Task | None = None
        self.cond = asyncio.Condition()

    async def push(self, event: dict) -> None:
        event = {"seq": len(self.events) + 1, "ts": _now(), **event}
        self.events.append(event)
        async with self.cond:
            self.cond.notify_all()

    @property
    def finished(self) -> bool:
        return self.status in FINAL

    async def follow(self, after: int = 0, heartbeat: float = 15.0) -> AsyncIterator[dict | None]:
        """Yield events after `after`; yields None as a heartbeat when idle.

        Ends once the incident is closed and everything has been delivered (a paused incident keeps the
        stream open: the run continues on the same log after a decision).
        """
        idx = max(0, after)
        while True:
            while idx < len(self.events):
                yield self.events[idx]
                idx += 1
            if self.finished:
                return
            try:
                async with self.cond:
                    await asyncio.wait_for(self.cond.wait_for(lambda: len(self.events) > idx), heartbeat)
            except asyncio.TimeoutError:
                yield None


class IncidentManager:
    def __init__(self, data_dir: Path | None = None, store=None) -> None:
        self.data_dir = data_dir
        self._injected_store = store
        self.handles: dict[str, IncidentHandle] = {}
        self._stack = AsyncExitStack()
        self.graph = None
        self.store = None
        self.checkpointer = None

    # ---------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        if self.data_dir:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            from langgraph.store.sqlite.aio import AsyncSqliteStore

            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.checkpointer = await self._stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.data_dir / "checkpoints.sqlite")))
            self.store = await self._stack.enter_async_context(
                AsyncSqliteStore.from_conn_string(str(self.data_dir / "memory.sqlite")))
            await self.store.setup()
        else:
            self.checkpointer, self.store = InMemorySaver(), self._injected_store or InMemoryStore()
        self.graph = build_graph(checkpointer=self.checkpointer, store=self.store)

    async def shutdown(self) -> None:
        for h in self.handles.values():
            if h.task and not h.task.done():
                h.task.cancel()
        await self._stack.aclose()

    # ------------------------------------------------------------------ queries
    def _cfg(self, incident_id: str) -> dict:
        return {"configurable": {"thread_id": incident_id}, "recursion_limit": 200}

    async def list(self) -> list[dict]:
        items = await self.store.asearch(RUNS_NS, limit=200)
        runs = [i.value for i in items]
        for r in runs:
            h = self.handles.get(r["incident_id"])
            if h:
                r["status"] = h.status
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return runs

    async def snapshot(self, incident_id: str) -> dict | None:
        st = await self.graph.aget_state(self._cfg(incident_id))
        h = self.handles.get(incident_id)
        if not st.values:
            if h is None:
                return None
            # The run was just created and has not checkpointed yet: answer from the live handle.
            return {"incident_id": incident_id, "status": h.status, "values": {"incident_id": incident_id},
                    "interrupt": None, "next": [], "has_events": bool(h.events), "last_seq": len(h.events),
                    "meta": h.meta}
        interrupt = st.interrupts[0].value if st.interrupts else None
        status = "awaiting_approval" if interrupt else st.values.get("status", "unknown")
        if h and h.status in ("running", "starting"):
            status = h.status
        return {"incident_id": incident_id, "status": status, "values": st.values, "interrupt": interrupt,
                "next": list(st.next), "has_events": bool(h and h.events), "last_seq": len(h.events) if h else 0,
                "meta": (h.meta if h else await self._meta(incident_id))}

    async def _meta(self, incident_id: str) -> dict:
        item = await self.store.aget(RUNS_NS, incident_id)
        return item.value if item else {"incident_id": incident_id}

    # -------------------------------------------------------------------- runs
    async def create(self, scenario: str, seed: int | None = None, settings: dict | None = None,
                     incident_id: str | None = None, created_at: datetime | None = None) -> IncidentHandle:
        spec = resolve(scenario, seed)
        ctx = RunContext(**{k: v for k, v in (settings or {}).items() if k in SETTINGS_KEYS and v is not None})
        inp = new_incident_input(asdict(spec), created_at=created_at, incident_id=incident_id)
        world = World(spec, anchor=datetime.fromisoformat(inp["created_at"]))
        register_world(inp["incident_id"], world)
        mode = resolve_mode(ctx.reasoning, ctx.model)
        engine = model_label(ctx.model) if mode == "llm" else "Deterministic reasoner (offline)"
        meta = {"incident_id": inp["incident_id"], "scenario": spec.id, "title": spec.title,
                "teaser": spec.description, "service": world.alert["service"], "alert": world.alert["title"],
                "created_at": _now(), "settings": asdict(ctx), "engine": engine, "status": "running",
                "severity": None}
        inp["settings"] = asdict(ctx)
        h = IncidentHandle(inp["incident_id"], meta)
        self.handles[h.id] = h
        await self.store.aput(RUNS_NS, h.id, meta)
        await h.push({"type": "run_started", "meta": meta})
        h.status = "running"
        h.task = asyncio.create_task(self._drive(h, inp, ctx))
        return h

    async def decide(self, incident_id: str, decision: dict) -> IncidentHandle:
        h = self.handles.get(incident_id) or await self._restore(incident_id)
        if h is None:
            raise KeyError(incident_id)
        st = await self.graph.aget_state(self._cfg(incident_id))
        if not st.interrupts or h.status == "running":
            raise RuntimeError("incident is not waiting for a decision")
        ctx = RunContext(**{k: v for k, v in (st.values.get("settings") or {}).items() if k in SETTINGS_KEYS})
        h.status = "running"
        h.interrupt = None
        await h.push({"type": "decision", "decision": decision})
        h.task = asyncio.create_task(self._drive(h, Command(resume=decision), ctx))
        return h

    async def _restore(self, incident_id: str) -> IncidentHandle | None:
        """Re-attach to an incident after a server restart (state lives in the checkpointer)."""
        st = await self.graph.aget_state(self._cfg(incident_id))
        if not st.values:
            return None
        forget_world(incident_id)  # rebuilt lazily from the checkpointed state (event sourcing)
        h = IncidentHandle(incident_id, await self._meta(incident_id))
        h.status = "awaiting_approval" if st.interrupts else st.values.get("status", "unknown")
        h.interrupt = st.interrupts[0].value if st.interrupts else None
        self.handles[incident_id] = h
        return h

    async def _drive(self, h: IncidentHandle, payload: Any, ctx: RunContext) -> None:
        cfg = self._cfg(h.id)
        try:
            async for ns, mode, data in self.graph.astream(payload, cfg, context=ctx,
                                                           stream_mode=["custom", "updates", "tasks"],
                                                           subgraphs=True):
                if mode == "custom":
                    await h.push(data)
                    if data.get("type") == "brief":
                        h.meta["severity"] = data["brief"].get("severity")
                elif mode == "tasks" and not ns:
                    if "result" in data or "error" in data:
                        err = data.get("error")
                        await h.push({"type": "node", "node": data["name"], "phase": "end", "task_id": data["id"],
                                      "error": str(err) if err else None})
                    else:
                        ev = {"type": "node", "node": data["name"], "phase": "start", "task_id": data["id"]}
                        if data["name"] == "investigate":
                            ev["investigator"] = ((data.get("input") or {}).get("task") or {}).get("id")
                        await h.push(ev)
                elif mode == "updates" and not ns:
                    for node, update in data.items():
                        if node != "__interrupt__" and isinstance(update, dict) and update.get("status"):
                            await h.push({"type": "status", "status": update["status"], "node": node})
            st = await self.graph.aget_state(cfg)
            if st.interrupts:
                h.status = "awaiting_approval"
                h.interrupt = st.interrupts[0].value
                await h.push({"type": "approval_required", "interrupt": h.interrupt})
            else:
                h.status = st.values.get("status", "done")
                await h.push({"type": "done", "status": h.status})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface failures to the UI instead of dying silently
            log.exception("incident %s failed", h.id)
            h.status = "failed"
            await h.push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            h.meta["status"] = h.status
            try:
                await self.store.aput(RUNS_NS, h.id, h.meta)
            except Exception:  # pragma: no cover
                pass
