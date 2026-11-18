"""Per-run configuration (LangGraph runtime context) and per-incident resources."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .sim.world import ScenarioSpec, World
from .tools import build_tools

DEFAULT_MODEL = os.environ.get("ARGUS_MODEL", "anthropic:claude-opus-5-5")


@dataclass
class RunContext:
    """Static configuration for one incident run (``context=`` in ``graph.astream``).

    Everything is JSON-friendly so the same graph runs unchanged under LangGraph
    Studio / LangGraph Platform, where context arrives as JSON.
    """

    reasoning: Literal["auto", "llm", "deterministic"] = "auto"
    model: str = DEFAULT_MODEL
    approval_policy: Literal["low", "medium", "high", "never"] = "medium"
    max_rounds: int = 3
    max_attempts: int = 2
    max_plan_revisions: int = 2
    max_tool_calls: int = 6
    pace: float = 0.0


def needs_approval(risk: str, policy: str) -> bool:
    """Does an action with `risk` need a human under `policy` (the lowest risk that needs approval)?"""
    order = {"low": 0, "medium": 1, "high": 2}
    if policy == "never":
        return False
    return order.get(risk, 2) >= order.get(policy, 1)


_worlds: dict[str, World] = {}
_tools: dict[str, dict] = {}
_lock = threading.Lock()


def world_ref(state: dict) -> dict:
    """The minimal state needed to rebuild an incident's world (event sourcing)."""
    return {k: state.get(k) for k in ("incident_id", "scenario", "created_at", "world_clock", "executed")}


def get_world(ref: dict) -> World:
    iid = ref["incident_id"]
    with _lock:
        w = _worlds.get(iid)
        if w is None:
            w = _rebuild(ref)
            _worlds[iid] = w
        return w


def register_world(incident_id: str, world: World) -> None:
    with _lock:
        _worlds[incident_id] = world
        _tools.pop(incident_id, None)


def forget_world(incident_id: str) -> None:
    with _lock:
        _worlds.pop(incident_id, None)
        _tools.pop(incident_id, None)


def get_tools(ref: dict) -> dict:
    iid = ref["incident_id"]
    world = get_world(ref)
    with _lock:
        tools = _tools.get(iid)
        if tools is None:
            tools = _tools[iid] = build_tools(world)
        return tools


def _rebuild(ref: dict) -> World:
    spec = ScenarioSpec(**ref["scenario"])
    created = datetime.fromisoformat(ref["created_at"]) if ref.get("created_at") else None
    world = World(spec, anchor=created)
    for a in ref.get("executed") or []:
        if a.get("ok") and a.get("t") is not None:
            world.set_clock(a["t"])
            world.apply_action(a["action"], a.get("params") or {}, action_id=a.get("action_id"))
    if ref.get("world_clock"):
        world.set_clock(ref["world_clock"])
    return world
