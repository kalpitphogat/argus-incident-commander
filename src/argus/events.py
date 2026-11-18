"""Custom stream events.

Nodes call :func:`emit` to push UI-facing events through LangGraph's ``custom``
stream mode (``get_stream_writer``).  Outside a streaming run it is a no-op, so
the same graph works under ``invoke()``, in tests and in LangGraph Studio.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.config import get_stream_writer


def emit(event_type: str, **data: Any) -> None:
    try:
        writer = get_stream_writer()
    except Exception:  # not inside a graph run
        return
    try:
        writer({"type": event_type, **data})
    except Exception:  # pragma: no cover - never let telemetry break the agent
        pass


async def pace(seconds: float) -> None:
    """Artificial delay used by the offline reasoner so humans can follow the run in the UI."""
    if seconds > 0:
        await asyncio.sleep(seconds)
