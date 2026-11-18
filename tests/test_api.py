"""HTTP API: create -> stream -> approve -> resolved, plus telemetry, postmortem, webhook and auth."""

import asyncio
import json

import httpx
import pytest

from argus.server import create_app


@pytest.fixture
async def client():
    app = create_app(None)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://argus") as c:
            c.app = app
            yield c


async def wait_status(c, iid, wanted, timeout=30):
    for _ in range(int(timeout / 0.1)):
        snap = (await c.get(f"/api/incidents/{iid}")).json()
        if snap["status"] in wanted:
            return snap
        await asyncio.sleep(0.1)
    raise AssertionError(f"status never reached {wanted}: {snap['status']}")


async def test_full_incident_over_http(client):
    c = client
    assert (await c.get("/api/health")).json()["llm_available"] is False
    shape = (await c.get("/api/graph")).json()
    assert {"dispatch", "investigate", "critique", "approval", "verify"} <= {n["id"] for n in shape["nodes"]}
    r = await c.post("/api/incidents", json={"scenario": "flash-sale-pool-exhaustion", "reasoning": "deterministic"})
    iid = r.json()["incident_id"]
    snap = await wait_status(c, iid, {"awaiting_approval"})
    assert snap["interrupt"]["plan"]["actions"][0]["action"] == "increase_connection_pool"
    assert (await c.post(f"/api/incidents/{iid}/decision", json={
        "decision": "approve", "by": "pytest", "edits": {"A1.1": {"max_connections": 600}}})).status_code == 200
    snap = await wait_status(c, iid, {"resolved", "mitigated", "escalated", "failed"})
    assert snap["status"] == "resolved"
    assert snap["values"]["executed"][0]["params"]["max_connections"] == 600  # the human's edit was applied
    tel = (await c.get(f"/api/incidents/{iid}/telemetry", params={"components": "checkout-service,orders-db"})).json()
    assert {"alert", "feature_flag", "action"} <= {m["kind"] for m in tel["markers"]}
    assert tel["components"]["orders-db"]["metrics"]["connections"]["limit_points"]
    pm = await c.get(f"/api/incidents/{iid}/postmortem.md")
    assert pm.status_code == 200 and "Root cause" in pm.text
    listed = (await c.get("/api/incidents")).json()["incidents"]
    assert listed[0]["incident_id"] == iid and listed[0]["severity"] == "SEV1"


async def test_event_stream_replays_in_order(client):
    c = client
    iid = (await c.post("/api/incidents", json={"scenario": "auth-cert-expiry", "reasoning": "deterministic",
                                                  "approval_policy": "never"})).json()["incident_id"]
    await wait_status(c, iid, {"resolved"})
    events = []
    async with c.stream("GET", f"/api/incidents/{iid}/events") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["type"] == "done":
                    break
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and seqs[0] == 1
    types = {e["type"] for e in events}
    assert {"node", "round", "tool_call", "tool_result", "finding", "hypotheses", "plan", "action",
            "verification", "report", "done"} <= types


async def test_decision_conflicts_and_unknowns(client):
    c = client
    assert (await c.get("/api/incidents/INC-NOPE")).status_code == 404
    assert (await c.post("/api/incidents", json={"scenario": "nope"})).status_code == 404
    iid = (await c.post("/api/incidents", json={"scenario": "cart-bad-release", "reasoning": "deterministic",
                                                  "approval_policy": "never"})).json()["incident_id"]
    await wait_status(c, iid, {"resolved"})
    r = await c.post(f"/api/incidents/{iid}/decision", json={"decision": "approve"})
    assert r.status_code == 409


async def test_alertmanager_webhook_and_token(client, monkeypatch):
    c = client
    monkeypatch.setenv("ARGUS_API_TOKEN", "s3cret")
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "HighErrorRate", "service": "payment-service",
                                                          "argus_policy": "never"}}]}
    assert (await c.post("/api/webhooks/alertmanager", json=payload)).status_code == 401
    r = await c.post("/api/webhooks/alertmanager", json=payload, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and len(r.json()["started"]) == 1
    snap = await wait_status(c, r.json()["started"][0], {"resolved", "mitigated"})
    assert snap["values"]["hypotheses"][0]["component"] == "payrail-api"


async def test_snapshot_is_served_before_the_first_checkpoint(client):
    """Regression: the UI opens the incident immediately after POST; it must never see a 404."""
    c = client
    iid = (await c.post("/api/incidents", json={"scenario": "cart-bad-release", "reasoning": "deterministic"})).json()["incident_id"]
    snap = await c.get(f"/api/incidents/{iid}")
    assert snap.status_code == 200 and snap.json()["incident_id"] == iid
    await wait_status(c, iid, {"awaiting_approval"})
