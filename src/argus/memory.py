"""Long-term incident memory on a LangGraph ``BaseStore``.

Every resolved incident is written as a compact record; triage recalls similar
past incidents for the components in the blast radius ("we have seen this
before: last time a rollback fixed it").
"""

from __future__ import annotations

from langgraph.store.base import BaseStore

NAMESPACE = ("argus", "incidents")


async def recall(store: BaseStore | None, components: list[str], limit: int = 3) -> list[dict]:
    if store is None:
        return []
    try:
        items = await store.asearch(NAMESPACE, limit=100)
    except Exception:  # pragma: no cover - memory must never break triage
        return []
    wanted = set(components)
    hits = [i.value for i in items if i.value.get("component") in wanted or i.value.get("service") in wanted]
    hits.sort(key=lambda v: v.get("closed_at", ""), reverse=True)
    return hits[:limit]


async def remember(store: BaseStore | None, incident_id: str, record: dict) -> None:
    if store is None:
        return
    try:
        await store.aput(NAMESPACE, incident_id, record)
    except Exception:  # pragma: no cover
        pass
