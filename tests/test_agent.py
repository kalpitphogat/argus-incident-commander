"""End-to-end behaviour of the incident-response graph (offline reasoner)."""

from dataclasses import asdict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from argus.context import RunContext, forget_world
from argus.graph import build_graph, new_incident_input
from argus.sim import CATALOG, BY_ID, World, resolve
from conftest import ANCHOR


@pytest.mark.parametrize("card", CATALOG, ids=lambda c: c.id)
async def test_finds_the_root_cause_and_fixes_it(card, run):
    values, interrupts = await run(card.spec())
    truth = World(card.spec(), ANCHOR).fault.ground_truth()
    top = values["hypotheses"][0]
    assert (top["category"], top["component"]) == (truth.category, truth.component)
    assert values["status"] in ("resolved", "mitigated")
    assert any(a["action"] in truth.fixing_actions for a in values["executed"] if a["ok"])
    assert values["report"]["postmortem"].startswith("# Postmortem")
    assert interrupts, "medium/high-risk actions must pause for a human"


@pytest.mark.parametrize("seed", range(100, 140))
async def test_random_unseen_faults(seed, run):
    spec = resolve("chaos", seed)
    values, _ = await run(spec, policy="never")
    truth = World(spec, ANCHOR).fault.ground_truth()
    top = values["hypotheses"][0]
    assert (top["category"], top["component"]) == (truth.category, truth.component), spec.fault
    assert values["status"] in ("resolved", "mitigated")


async def test_critic_sends_investigators_back_for_evidence(run):
    events = []
    values, _ = await run(BY_ID["checkout-deploy-regression"].spec(), collect=events)
    critiques = [e["critique"] for e in events if e["type"] == "critique"]
    assert values["round"] == 2 and not critiques[0]["proceed"] and critiques[-1]["proceed"]
    assert {t["target"] for t in values["task_log"] if t["round"] == 2} == {"orders-db"}
    # parallel fan-out really happened: round 1 had one task per specialist
    assert sorted(t["analyst"] for t in values["task_log"] if t["round"] == 1) == \
        ["changes", "dependencies", "logs", "metrics"]


async def test_rejection_with_feedback_replans(run):
    values, interrupts = await run(BY_ID["payment-provider-outage"].spec(), decisions=[
        {"decision": "reject", "feedback": "Failover changes our fees; contain it first.", "by": "sre"}])
    actions = [a["action"] for a in values["executed"]]
    assert interrupts[0]["plan"]["actions"][0]["action"] == "toggle_feature_flag"
    assert actions[0] == "enable_circuit_breaker"  # respected the human's feedback
    assert values["verifications"][0]["status"] == "partially_mitigated"  # latency fixed, errors not
    assert values["status"] == "mitigated" and actions[-1] == "toggle_feature_flag"
    assert any("Human reviewer" in f for f in values["feedback"])


async def test_temporary_fix_is_detected_and_real_fix_reproposed(run):
    values, interrupts = await run(BY_ID["recommendations-memory-leak"].spec(), policy="low", decisions=[
        {"decision": "approve", "approved_ids": ["A1.1"], "by": "sre"}])  # restart only, not the rollback
    statuses = [v["status"] for v in values["verifications"]]
    assert statuses[0] == "temporarily_mitigated" and statuses[-1] == "resolved"
    assert [a["action"] for a in values["executed"]] == ["restart_service", "rollback_deploy"]
    assert len(interrupts) == 2


async def test_reject_without_feedback_escalates(run):
    values, _ = await run(BY_ID["cart-bad-release"].spec(), decisions=[{"decision": "reject", "by": "sre"}])
    assert values["status"] == "escalated" and not values.get("executed")
    assert values["report"]["postmortem"]


async def test_autonomous_policy_never_interrupts(run):
    values, interrupts = await run(BY_ID["auth-cert-expiry"].spec(), policy="never")
    assert not interrupts and values["status"] == "resolved"


async def test_low_risk_actions_are_auto_approved(run):
    values, interrupts = await run(BY_ID["recommendations-memory-leak"].spec(), policy="medium")
    assert interrupts[0]["auto_approved"] == ["A1.1"]  # restart is low risk
    restart = next(a for a in values["executed"] if a["action"] == "restart_service")
    assert restart["approved_by"] == "policy (auto)"


async def test_memory_store_recalls_past_incidents(run):
    store = InMemoryStore()
    await run(BY_ID["cart-bad-release"].spec(), store=store)
    values, _ = await run(BY_ID["cart-bad-release"].spec(seed=99), store=store)
    assert values["memories"] and values["memories"][0]["category"] == "bad_deploy"


async def test_paused_incident_survives_a_restart(tmp_path):
    """Pause on interrupt, drop every in-memory object, resume from the SQLite checkpoint alone."""
    db = str(tmp_path / "ckpt.sqlite")
    spec = BY_ID["flash-sale-pool-exhaustion"].spec()
    inp = new_incident_input(asdict(spec), created_at=ANCHOR)
    ctx = RunContext(reasoning="deterministic")
    cfg = {"configurable": {"thread_id": inp["incident_id"]}, "recursion_limit": 200}
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        app = build_graph(checkpointer=saver)
        await app.ainvoke(inp, cfg, context=ctx)
        assert (await app.aget_state(cfg)).interrupts
    forget_world(inp["incident_id"])  # "server restart": the simulated world is gone too
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        app = build_graph(checkpointer=saver)
        await app.ainvoke(Command(resume={"decision": "approve", "by": "next-day"}), cfg, context=ctx)
        values = (await app.aget_state(cfg)).values
    forget_world(inp["incident_id"])
    assert values["status"] == "resolved"
    assert values["executed"][0]["approved_by"] == "next-day"
