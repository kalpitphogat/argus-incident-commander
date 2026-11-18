"""Reasoning engines and the factory that picks one per run."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..events import emit
from .base import Brain, has_credentials
from .deterministic import DeterministicBrain

log = logging.getLogger("argus.brains")

_cache: dict[tuple, Brain] = {}
_lock = threading.Lock()


class ResilientBrain(Brain):
    """Primary (LLM) brain with a per-call fallback to the deterministic reasoner."""

    def __init__(self, primary: Brain, fallback: Brain) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.label = primary.label

    def __getattribute__(self, item: str) -> Any:
        if item in ("triage", "next_step", "summarize", "hypothesize", "critique", "plan", "report"):
            primary = object.__getattribute__(self, "primary")
            fallback = object.__getattribute__(self, "fallback")

            async def call(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await getattr(primary, item)(*args, **kwargs)
                except Exception as exc:  # degrade, don't die
                    log.warning("LLM %s failed (%s); using deterministic fallback", item, exc)
                    emit("log", agent="Engine", level="warn",
                         text=f"{primary.label} failed on '{item}' ({type(exc).__name__}: {str(exc)[:160]}). "
                              f"Falling back to the deterministic reasoner for this step.")
                    return await getattr(fallback, item)(*args, **kwargs)

            return call
        return object.__getattribute__(self, item)

    # abstract methods are served dynamically by __getattribute__
    async def triage(self, inp): ...  # pragma: no cover
    async def next_step(self, task, brief, messages, tools, step): ...  # pragma: no cover
    async def summarize(self, task, brief, messages): ...  # pragma: no cover
    async def hypothesize(self, inp): ...  # pragma: no cover
    async def critique(self, inp): ...  # pragma: no cover
    async def plan(self, inp): ...  # pragma: no cover
    async def report(self, inp, on_token=None): ...  # pragma: no cover


def resolve_mode(reasoning: str, model: str) -> str:
    if reasoning == "auto":
        return "llm" if has_credentials(model) else "deterministic"
    return reasoning


def get_brain(ctx: Any) -> Brain:
    """One brain per (mode, model, pace); safe to share across incidents."""
    mode = resolve_mode(getattr(ctx, "reasoning", "auto"), getattr(ctx, "model", ""))
    key = (mode, getattr(ctx, "model", ""), getattr(ctx, "pace", 0.0))
    with _lock:
        brain = _cache.get(key)
        if brain is None:
            det = DeterministicBrain(pace_seconds=getattr(ctx, "pace", 0.0))
            if mode == "llm":
                from .llm import LLMBrain

                brain = ResilientBrain(LLMBrain(ctx.model), det)
            else:
                brain = det
            _cache[key] = brain
        return brain


__all__ = ["Brain", "DeterministicBrain", "ResilientBrain", "get_brain", "resolve_mode"]
