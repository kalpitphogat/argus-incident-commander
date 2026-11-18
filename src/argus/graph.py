"""The Argus incident-response graph (LangGraph).

    intake -> triage -> dispatch ==Send==> investigate (subgraph, xN in parallel)
                           ^                     |
                           |                 synthesize -> critique --(needs more)--> dispatch
                           |                                   |
                           |                                 plan <------------------------+
                           |                                   |                           |
                           |                     approval (interrupt: human-in-the-loop)   |
                           |                                   |                           |
                           |                     execute -> verify --(not recovered)-------+
                           |                                   |
                           +---------------------------------- report -> END

* parallel specialists via ``Send`` (map-reduce), each a tool-calling subgraph
  that compresses its work into a ``Finding`` (open_deep_research style);
* a reflection loop: a critic finds evidence gaps and dispatches follow-ups
  (gemini-fullstack-langgraph style);
* a human approval gate with ``interrupt()`` before risky actions (Agent Inbox
  style) and a closed loop that verifies recovery and re-plans if needed.
"""

from __future__ import annotations

import asyncio
import operator
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send, interrupt

from . import memory
from .brains import get_brain
from .brains.base import CritiqueInput, PlanInput, ReportInput, SynthesisInput, TriageInput
from .context import RunContext, get_tools, get_world, needs_approval, world_ref
from .events import emit, pace
from .knowledge import RunbookRetriever, runbook_for_category
from .schemas import ANALYST_LABEL, Finding, InvestigationTask
from .sim import resolve
from .sim.world import ACTION_CATALOG, RISK_ORDER, World
from .tools import ANALYST_TOOLS, _signals_text, signals

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class IncidentState(TypedDict, total=False):
    scenario_id: str  # convenience input (LangGraph Studio): a scenario id or "chaos"
    seed: int
    incident_id: str
    scenario: dict
    settings: dict
    created_at: str
    world_clock: int
    alert: dict
    status: str
    engine: str
    brief: dict
    memories: list[dict]
    round: int
    pending_tasks: list[dict]
    tasks: list[dict]
    task_log: Annotated[list[dict], operator.add]
    findings: Annotated[list[dict], operator.add]
    hypotheses: list[dict]
    critique: dict
    diagnosed_t: int
    plan: dict
    plan_revisions: int
    approval: dict
    executed: Annotated[list[dict], operator.add]
    verification: dict
    verifications: Annotated[list[dict], operator.add]
    remediation_attempts: int
    feedback: Annotated[list[str], operator.add]
    timeline: Annotated[list[dict], operator.add]
    report: dict


class InvestigatorState(TypedDict, total=False):
    task: dict
    brief: dict
    round: int
    ref: dict
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    findings: Annotated[list[dict], operator.add]


class InvestigatorOutput(TypedDict):
    findings: Annotated[list[dict], operator.add]


def _tl(world: World, kind: str, text: str, t: int | None = None, **extra: Any) -> dict:
    t = world.clock if t is None else t
    return {"t": t, "time": world.iso(t), "kind": kind, "text": text, **extra}


def _log(agent: str, text: str, level: str = "info", **extra: Any) -> None:
    emit("log", agent=agent, text=text, level=level, **extra)


def _engine(runtime: Runtime[RunContext]):
    return get_brain(runtime.context)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def intake(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    opened: dict = {}
    if not state.get("scenario"):  # minimal input, e.g. {"scenario_id": "flash-sale-pool-exhaustion"}
        spec = resolve(state.get("scenario_id") or "chaos", state.get("seed"))
        base = new_incident_input(asdict(spec), incident_id=state.get("incident_id"))
        opened = {k: base[k] for k in ("incident_id", "scenario", "created_at")}
        state = {**state, **opened}
    world = get_world(world_ref(state))
    a = world.alert
    alert = {"id": a["id"], "title": a["title"], "service": a["service"], "metric": a["metric"],
             "value": a["value"], "threshold": a["threshold"], "rule": a["rule"], "fired_at": world.iso(a["fired_t"])}
    brain = _engine(runtime)
    _log("Pager", f"[FIRING] {alert['title']} ({alert['rule']})", level="alert")
    emit("incident", alert=alert, engine=brain.label, world=world.public_state())
    return {
        **opened,
        "alert": alert, "status": "triaging", "engine": brain.label, "world_clock": world.clock,
        "round": 0, "remediation_attempts": 0, "plan_revisions": 0,
        "timeline": [_tl(world, "alert", f"Alert fired: {alert['title']}", a["fired_t"]),
                     _tl(world, "engaged", f"Argus paged and took incident command ({brain.label})")],
    }


async def triage(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    ref = world_ref(state)
    world = get_world(ref)
    alert = state["alert"]
    svc = alert["service"]
    topo = world.topology
    sig = signals(world, svc, 60)
    blast = topo.upstream(svc)
    deps = [e.callee for e in topo.callees(svc)]
    memories = await memory.recall(runtime.store, [svc, *blast, *deps])
    _log("Triage", f"Assessing {svc}: pulling golden signals, blast radius and similar past incidents.")
    if memories:
        _log("Memory", f"Recalled {len(memories)} similar past incident(s): "
                       + "; ".join(f"{m['incident_id']} - {m['root_cause']}" for m in memories))
    brain = _engine(runtime)
    draft = await brain.triage(TriageInput(alert=alert, signals=sig, signals_text=_signals_text(world, sig),
                                           blast_radius=blast, dependencies=deps, memories=memories,
                                           topology_text=topology_text(world)))
    tasks = [t.model_dump() for t in draft.initial_tasks if t.target in topo][:4]
    if not tasks:  # never start without work
        tasks = [InvestigationTask(analyst=a, target=svc, objective=f"Investigate {svc} ({a}).").model_dump()
                 for a in ANALYST_TOOLS]
    world.set_clock(state["world_clock"] + 1)
    brief = {**draft.model_dump(exclude={"initial_tasks"}), "service": svc, "blast_radius": blast}
    _log("Triage", f"{draft.severity}: {draft.summary}", level="decision")
    emit("brief", brief=brief)
    return {"brief": brief, "memories": memories, "pending_tasks": tasks, "status": "investigating",
            "world_clock": world.clock,
            "timeline": [_tl(world, "triage", f"Triaged as {draft.severity}: {draft.title}")]}


async def dispatch(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    rnd = state.get("round", 0) + 1
    tasks = []
    for i, t in enumerate(state.get("pending_tasks") or [], 1):
        tasks.append({**t, "id": f"R{rnd}-T{i}", "round": rnd})
    labels = ", ".join(f"{ANALYST_LABEL[t['analyst']]} -> {t['target']}" for t in tasks)
    _log("Commander", f"Round {rnd}: dispatching {len(tasks)} investigators in parallel ({labels}).",
         level="decision")
    emit("round", round=rnd, tasks=tasks)
    return {"round": rnd, "tasks": tasks, "task_log": tasks, "pending_tasks": [],
            "timeline": [_tl(world, "round", f"Investigation round {rnd}: {len(tasks)} parallel investigators "
                                             f"({labels})")]}


def fan_out(state: IncidentState) -> list[Send]:
    ref = world_ref(state)
    return [Send("investigate", {"task": t, "brief": state.get("brief", {}), "round": state.get("round", 1),
                                 "ref": ref, "messages": [], "steps": 0}) for t in state.get("tasks", [])]


# --- investigator subgraph ---------------------------------------------------


async def inv_agent(state: InvestigatorState, runtime: Runtime[RunContext]) -> dict:
    task = InvestigationTask(**{k: v for k, v in state["task"].items() if k in InvestigationTask.model_fields})
    tid = state["task"]["id"]
    tools_all = get_tools(state["ref"])
    tools = [tools_all[n] for n in ANALYST_TOOLS[task.analyst]]
    brain = _engine(runtime)
    if state.get("steps", 0) == 0:
        emit("investigator", task_id=tid, analyst=task.analyst, target=task.target, status="running",
             objective=task.objective)
    msg = await brain.next_step(task, state.get("brief", {}), state.get("messages", []), tools,
                                state.get("steps", 0))
    text = msg.text if isinstance(msg.text, str) else ""
    if text.strip():
        emit("thought", task_id=tid, analyst=task.analyst, text=text.strip()[:600])
    for tc in msg.tool_calls or []:
        emit("tool_call", task_id=tid, analyst=task.analyst, tool=tc["name"], args=tc.get("args", {}),
             call_id=tc["id"])
    return {"messages": [msg], "steps": state.get("steps", 0) + 1}


async def inv_tools(state: InvestigatorState, runtime: Runtime[RunContext]) -> dict:
    last = state["messages"][-1]
    analyst = state["task"]["analyst"]
    tools_all = get_tools(state["ref"])
    allowed = set(ANALYST_TOOLS[analyst])

    async def run(tc: dict) -> ToolMessage:
        name = tc["name"]
        if name not in allowed or name not in tools_all:
            return ToolMessage(content=f"Tool '{name}' is not available to the {analyst} analyst. "
                                       f"Available: {', '.join(sorted(allowed))}.",
                               tool_call_id=tc["id"], name=name, status="error")
        try:
            return await tools_all[name].ainvoke({**tc, "type": "tool_call"})
        except Exception as exc:  # tool errors go back to the investigator, they don't crash the run
            return ToolMessage(content=f"Tool error: {exc}", tool_call_id=tc["id"], name=name, status="error")

    results = await asyncio.gather(*(run(tc) for tc in last.tool_calls))
    for tc, res in zip(last.tool_calls, results):
        art = getattr(res, "artifact", None)
        emit("tool_result", task_id=state["task"]["id"], analyst=analyst, tool=tc["name"], call_id=tc["id"],
             status=getattr(res, "status", "success"), preview=str(res.content)[:700],
             charts=_chart_refs(tc["name"], art))
    return {"messages": list(results)}


def inv_route(state: InvestigatorState, runtime: Runtime[RunContext]) -> Literal["tools", "summarize"]:
    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and last.tool_calls and state.get("steps", 0) <= runtime.context.max_tool_calls:
        return "tools"
    return "summarize"


async def inv_summarize(state: InvestigatorState, runtime: Runtime[RunContext]) -> dict:
    task_d = state["task"]
    task = InvestigationTask(**{k: v for k, v in task_d.items() if k in InvestigationTask.model_fields})
    brain = _engine(runtime)
    draft = await brain.summarize(task, state.get("brief", {}), state.get("messages", []))
    calls = sum(len(m.tool_calls or []) for m in state.get("messages", []) if isinstance(m, AIMessage))
    finding = Finding(**draft.model_dump(), task_id=task_d["id"], analyst=task.analyst, target=task.target,
                      round=state.get("round", 1), tool_calls=calls, engine=brain.label).model_dump()
    emit("finding", finding=finding)
    emit("investigator", task_id=task_d["id"], analyst=task.analyst, target=task.target, status="done",
         headline=finding["headline"])
    return {"findings": [finding]}


def build_investigator() -> Any:
    g = StateGraph(InvestigatorState, context_schema=RunContext, output_schema=InvestigatorOutput)
    g.add_node("agent", inv_agent, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("tools", inv_tools)
    g.add_node("summarize", inv_summarize, retry_policy=RetryPolicy(max_attempts=2))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", inv_route, ["tools", "summarize"])
    g.add_edge("tools", "agent")
    g.add_edge("summarize", END)
    return g.compile(name="investigator")


# --- reasoning ---------------------------------------------------------------


async def synthesize(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    rnd = state.get("round", 1)
    findings = state.get("findings", [])
    _log("RCA lead", f"Synthesising {len(findings)} findings into ranked root-cause hypotheses.")
    brain = _engine(runtime)
    hs = await brain.hypothesize(SynthesisInput(brief=state.get("brief", {}), findings=findings,
                                                memories=state.get("memories", []),
                                                topology_text=topology_text(world),
                                                previous=state.get("hypotheses", [])))
    hyps = [h.model_dump() for h in hs.hypotheses if h.component in world.topology][:4]
    for h in hyps:
        h["confidence"] = max(0.0, min(1.0, float(h["confidence"])))
    world.set_clock(state["world_clock"] + 2)
    emit("hypotheses", hypotheses=hyps, summary=hs.summary, round=rnd)
    if hyps:
        _log("RCA lead", f"H1 ({hyps[0]['confidence']:.0%}): {hyps[0]['title']}", level="decision")
    return {"hypotheses": hyps, "world_clock": world.clock}


async def critique(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    ctx = runtime.context
    brain = _engine(runtime)
    _log("Critic", "Challenging the leading hypothesis: is the evidence enough to act on?")
    c = await brain.critique(CritiqueInput(brief=state.get("brief", {}), hypotheses=state.get("hypotheses", []),
                                           findings=state.get("findings", []), task_log=state.get("task_log", []),
                                           round=state.get("round", 1), max_rounds=ctx.max_rounds,
                                           components=list(world.topology.components)))
    follow = [t.model_dump() for t in c.follow_up_tasks if t.target in world.topology][:4]
    more = c.verdict == "needs_more_evidence" and follow and state.get("round", 1) < ctx.max_rounds
    out: dict = {"critique": {**c.model_dump(), "follow_up_tasks": follow, "proceed": not more}}
    emit("critique", critique=out["critique"], round=state.get("round", 1))
    if more:
        _log("Critic", c.reasoning, level="warn")
        out["pending_tasks"] = follow
    else:
        _log("Critic", c.reasoning, level="decision")
        top = (state.get("hypotheses") or [{}])[0]
        out["diagnosed_t"] = world.clock
        out["status"] = "planning"
        if top:
            out["timeline"] = [_tl(world, "diagnosis", f"Root cause identified ({top.get('confidence', 0):.0%}): "
                                                       f"{top.get('title')}")]
    return out


def after_critique(state: IncidentState) -> Literal["dispatch", "plan"]:
    return "plan" if state.get("critique", {}).get("proceed", True) else "dispatch"


# --- remediation ------------------------------------------------------------


def planning_facts(world: World, hypothesis: dict, state: dict) -> dict:
    topo = world.topology
    comp = hypothesis.get("component")
    facts: dict[str, Any] = {"component": comp}
    if comp in topo:
        c = topo.get(comp)
        facts.update(kind=c.kind, team=c.team, running_version=world.versions.get(comp))
        hist = sorted(world.deploy_history.get(comp, []), key=lambda h: h["t"])
        cur = world.versions.get(comp)
        facts["previous_version"] = next((h["from_version"] for h in reversed(hist) if h["to_version"] == cur), None)
        if c.max_connections:
            current = world.max_connections.get(comp, c.max_connections)
            facts["max_connections"] = current
            facts["suggested_max_connections"] = min(2000, current * 2)
        facts["callers"] = [e.caller for e in topo.callers(comp)]
        facts["top_caller"] = facts["callers"][0] if facts["callers"] else None
        facts["replicas"] = world.replicas.get(comp)
    cid = hypothesis.get("change_id")
    if cid:
        ch = next((c for c in world.changes if c.id == cid), None)
        if ch:
            facts["change"] = {"id": ch.id, "kind": ch.kind, "component": ch.component, "summary": ch.summary,
                               "details": ch.details}
            if ch.kind == "feature_flag":
                facts["flag"] = ch.details.get("flag")
    certs = [c for c in world.certificate_list() if c["owner"] == comp and (c["expired"] or c["expires_in_hours"] < 1)]
    if certs:
        facts["certificate"] = certs[0]["name"]
    facts["certificates"] = [c for c in world.certificate_list() if c["owner"] == comp][:4]
    facts["feature_flags"] = {k: v["enabled"] for k, v in world.flags.items()}
    # Actions the human just declined are not re-proposed -- unless a verification has happened since,
    # in which case there is new evidence and the human gets to decide again.
    ap = state.get("approval") or {}
    ver = state.get("verification") or {}
    fresh_rejection = ap and (not ver or ap.get("t", 0) > ver.get("t", -1))
    facts["rejected_actions"] = ap.get("rejected_actions", []) if fresh_rejection else []
    return facts


async def plan(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    hyps = state.get("hypotheses") or []
    verifs = state.get("verifications", [])
    # after a failed fix of H1 with nothing left to try, move to the next hypothesis
    hypothesis = hyps[0] if hyps else {}
    if verifs and len(hyps) > 1 and state.get("remediation_attempts", 0) >= 2:
        hypothesis = hyps[1]
    facts = planning_facts(world, hypothesis, state)
    runbooks = []
    seen = set()
    candidates = runbook_for_category(hypothesis.get("category", "")) or RunbookRetriever(k=2).invoke(
        f"{hypothesis.get('category', '')} {hypothesis.get('title', '')}")
    for d in candidates:
        if d.metadata["id"] in seen or not d.metadata.get("actions"):
            continue
        seen.add(d.metadata["id"])
        runbooks.append({"id": d.metadata["id"], "title": d.metadata["title"], "content": d.page_content,
                         "actions": d.metadata.get("actions", [])})
    _log("Planner", f"Drafting remediation for: {hypothesis.get('title', 'unknown cause')} "
                    f"(runbooks: {', '.join(r['id'] for r in runbooks) or 'none'}).")
    brain = _engine(runtime)
    draft = await brain.plan(PlanInput(brief=state.get("brief", {}), hypotheses=hyps, hypothesis=hypothesis,
                                       facts=facts, runbooks=runbooks, action_catalog=ACTION_CATALOG,
                                       executed=state.get("executed", []), feedback=state.get("feedback", [])))
    revision = state.get("plan_revisions", 0) + (1 if state.get("plan") else 0)
    actions = []
    for i, a in enumerate(draft.actions[:3], 1):
        risk = a.risk
        floor = ACTION_CATALOG[a.action]["risk"]
        if RISK_ORDER[risk] < RISK_ORDER[floor]:
            risk = floor  # guardrail: the model can raise risk, never lower it
        actions.append({"id": f"A{revision + 1}.{i}", "action": a.action, "params": a.params.compact(),
                        "rationale": a.rationale, "risk": risk, "expected_effect": a.expected_effect})
    plan_d = {**draft.model_dump(exclude={"actions"}), "actions": actions, "hypothesis_id": hypothesis.get("id"),
              "hypothesis": hypothesis.get("title"), "revision": revision + 1}
    world.set_clock(state["world_clock"] + 1)
    emit("plan", plan=plan_d)
    _log("Planner", plan_d["summary"], level="decision")
    return {"plan": plan_d, "plan_revisions": revision, "world_clock": world.clock,
            "status": "awaiting_approval" if actions else "escalated",
            "timeline": [_tl(world, "plan", f"Remediation plan v{revision + 1}: {plan_d['summary']}")]}


def after_plan(state: IncidentState) -> Literal["approval", "report"]:
    return "approval" if state.get("plan", {}).get("actions") else "report"


async def approval(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    """Human-in-the-loop gate. Interrupts for any action at/above the approval policy."""
    plan_d = state["plan"]
    policy = runtime.context.approval_policy
    actions = plan_d["actions"]
    gated = [a for a in actions if needs_approval(a["risk"], policy)]
    auto = [a for a in actions if a not in gated]
    decision: dict[str, Any] = {"decision": "approve", "approved": [a["id"] for a in actions],
                                "by": "approval policy (auto)", "comment": "", "edits": {}}
    if gated:
        top = (state.get("hypotheses") or [{}])[0]
        response = interrupt({
            "kind": "approval",
            "question": f"Approve {len(gated)} action(s) to mitigate: {top.get('title', '')}?",
            "plan": plan_d, "gated": [a["id"] for a in gated], "auto_approved": [a["id"] for a in auto],
            "policy": policy, "hypothesis": top, "severity": state.get("brief", {}).get("severity"),
        })
        decision = _normalize_decision(response, actions, gated, auto)
    world = get_world(world_ref(state))
    approved_ids = set(decision["approved"])
    rejected = [a["action"] for a in actions if a["id"] not in approved_ids]
    decision["rejected_actions"] = rejected
    world.set_clock(state["world_clock"] + (2 if gated else 0))
    decision["t"] = world.clock
    emit("approval", decision=decision)
    who = decision["by"]
    if decision["decision"] == "approve" and approved_ids:
        text = f"{who} approved {len(approved_ids)} action(s)" + (f": {decision['comment']}" if decision["comment"] else "")
        _log("Approval", text, level="decision")
        status = "mitigating"
    else:
        text = f"{who} rejected the plan" + (f": {decision.get('feedback') or decision['comment']}"
                                             if decision.get("feedback") or decision["comment"] else "")
        _log("Approval", text, level="warn")
        status = "replanning"
    out: dict = {"approval": decision, "status": status, "world_clock": world.clock,
                 "timeline": [_tl(world, "approval", text)]}
    if decision.get("feedback"):
        out["feedback"] = [f"Human reviewer: {decision['feedback']}"]
    return out


def _normalize_decision(resp: Any, actions: list[dict], gated: list[dict], auto: list[dict]) -> dict:
    resp = resp if isinstance(resp, dict) else {"decision": "approve" if resp in (True, "approve", "yes") else "reject"}
    kind = str(resp.get("decision", "approve")).lower()
    by = resp.get("by") or "on-call engineer"
    comment = resp.get("comment") or ""
    edits = resp.get("edits") or {}
    for a in actions:
        if a["id"] in edits and isinstance(edits[a["id"]], dict):
            a["params"] = {**a["params"], **{k: v for k, v in edits[a["id"]].items() if v not in (None, "")}}
    if kind in ("reject", "deny", "no"):
        return {"decision": "reject", "approved": [], "by": by, "comment": comment,
                "feedback": resp.get("feedback") or comment, "edits": edits}
    chosen = resp.get("approved_ids")
    if chosen is None:
        chosen = [a["id"] for a in gated]
    approved = [a["id"] for a in auto] + [a["id"] for a in gated if a["id"] in set(chosen)]
    return {"decision": "approve" if approved else "reject", "approved": approved, "by": by, "comment": comment,
            "feedback": resp.get("feedback") or "", "edits": edits}


def after_approval(state: IncidentState, runtime: Runtime[RunContext]) -> Literal["execute", "plan", "report"]:
    ap = state.get("approval", {})
    if ap.get("decision") == "approve" and ap.get("approved"):
        return "execute"
    if ap.get("feedback") and state.get("plan_revisions", 0) < runtime.context.max_plan_revisions:
        return "plan"
    return "report"


async def execute(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    approved = set(state["approval"]["approved"])
    results = []
    timeline = []
    clock = state["world_clock"]
    for a in state["plan"]["actions"]:
        if a["id"] not in approved:
            continue
        clock += 1
        world.set_clock(clock)
        _log("Executor", f"Executing {a['action']} {a['params']} ...")
        await pace(runtime.context.pace)
        res = world.apply_action(a["action"], a["params"], action_id=f"{state['incident_id']}:{a['id']}")
        rec = {"action_id": a["id"], "action": a["action"], "params": res.get("params", a["params"]),
               "ok": res["ok"], "message": res["message"], "change_id": res.get("change_id"), "t": world.clock,
               "time": world.iso(world.clock), "risk": a["risk"], "approved_by": state["approval"]["by"]
               if needs_approval(a["risk"], runtime.context.approval_policy) else "policy (auto)"}
        results.append(rec)
        emit("action", result=rec)
        _log("Executor", f"{'OK' if res['ok'] else 'FAILED'}: {a['action']} - {res['message']}",
             level="decision" if res["ok"] else "error")
        timeline.append(_tl(world, "action", f"{'Executed' if res['ok'] else 'Failed'} {a['action']} "
                                             f"{rec['params']} ({rec['approved_by']})"))
    return {"executed": results, "world_clock": world.clock, "status": "verifying", "timeline": timeline}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def verify(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    observe = 8
    start_clock = state["world_clock"]
    _log("Verifier", f"Observing the system for {observe} minutes after the change ...")
    await pace(runtime.context.pace * 2)
    world.set_clock(start_clock + observe)
    topo = world.topology
    alert = state["alert"]
    svc = alert["service"]
    comp = topo.get(svc)
    hyps = state.get("hypotheses") or [{}]
    root = hyps[0].get("component", svc)
    win = (world.clock - 4, world.clock)
    before = (start_clock - 6, start_clock - 1)
    checks = []

    def check(component: str, metric: str, limit: float, label: str) -> dict:
        now_v = _mean([v for _, v in world.series(component, metric, *win)])
        was_v = _mean([v for _, v in world.series(component, metric, *before)])
        c = {"component": component, "metric": metric, "label": label, "before": round(was_v, 2),
             "after": round(now_v, 2), "limit": limit, "ok": now_v <= limit}
        checks.append(c)
        return c

    if comp.slo_error_pct is not None:
        check(svc, "error_rate_pct", comp.slo_error_pct, f"{svc} error rate <= {comp.slo_error_pct:g}% (SLO)")
    if comp.slo_latency_ms is not None:
        check(svc, "latency_p99_ms", comp.slo_latency_ms, f"{svc} p99 <= {comp.slo_latency_ms:g} ms (SLO)")
    leak = None
    root_external = root in topo and topo.get(root).kind == "external"
    if root in topo and root != svc:
        rc = topo.get(root)
        if rc.kind == "service" and rc.slo_error_pct is not None:
            check(root, "error_rate_pct", rc.slo_error_pct, f"{root} error rate <= {rc.slo_error_pct:g}%")
        if rc.max_connections:
            cap = world.max_connections.get(root, rc.max_connections)
            check(root, "connections", cap * 0.9, f"{root} connections < 90% of max ({cap})")
    if root in topo and topo.get(root).kind == "service":
        mem = [v for _, v in world.series(root, "memory_pct", world.clock - 6, world.clock)]
        if len(mem) >= 4:
            slope = (mem[-1] - mem[0]) / (len(mem) - 1)
            base = topo.get(root).memory_pct
            if slope > 0.9:
                eta = (100 - mem[-1]) / slope
                leak = {"slope": round(slope, 2), "current": round(mem[-1], 1), "eta_minutes": round(eta)}
                checks.append({"component": root, "metric": "memory_pct", "label": f"{root} memory stable",
                               "before": round(base, 1), "after": round(mem[-1], 1), "limit": None, "ok": False,
                               "note": f"growing {slope:.1f}%/min - next OOM in ~{eta:.0f} min"})
    user_ok = all(c["ok"] for c in checks if c["component"] == svc)
    all_ok = all(c["ok"] for c in checks)
    if user_ok and all_ok:
        status, recovered = ("mitigated" if root_external else "resolved"), True
    elif user_ok and leak:
        status, recovered = "temporarily_mitigated", False
    elif user_ok:
        status, recovered = "mitigated", True
    else:
        improved = [c for c in checks if c["component"] == svc and c["after"] < c["before"] * 0.5]
        worse = [c for c in checks if c["component"] == svc and c["after"] > c["before"] * 1.2]
        status = "partially_mitigated" if improved else ("worse" if worse else "not_improved")
        recovered = False
    summary = {
        "resolved": "All SLO checks are green and the root component is healthy.",
        "mitigated": "Customer impact is gone; the root component itself is still degraded (outside our control) "
                     "or only mitigated.",
        "temporarily_mitigated": f"Symptoms are gone for now, but {root}'s memory is still climbing "
                                 f"({leak['slope'] if leak else '?'}%/min): the fix was temporary.",
        "partially_mitigated": "Some signals improved but the SLO is still breached.",
        "not_improved": "No measurable improvement: the action did not address the cause.",
        "worse": "Signals got worse after the change.",
    }[status]
    attempts = state.get("remediation_attempts", 0) + 1
    v = {"status": status, "recovered": recovered, "checks": checks, "summary": summary, "attempt": attempts,
         "leak": leak, "t": world.clock, "time": world.iso(world.clock)}
    emit("verification", verification=v)
    emit("world", world=world.public_state())
    _log("Verifier", f"{status.replace('_', ' ').upper()}: {summary}", level="decision" if recovered else "warn")
    out: dict = {"verification": v, "verifications": [v], "remediation_attempts": attempts,
                 "world_clock": world.clock, "status": "resolved" if recovered else "replanning",
                 "timeline": [_tl(world, "verify", f"Verification: {status.replace('_', ' ')} - {summary}")]}
    if not recovered:
        out["feedback"] = [f"Verification after attempt {attempts}: {status} - {summary}"]
    return out


def after_verify(state: IncidentState, runtime: Runtime[RunContext]) -> Literal["plan", "report"]:
    v = state.get("verification", {})
    if v.get("recovered"):
        return "report"
    return "plan" if state.get("remediation_attempts", 0) < runtime.context.max_attempts + 1 else "report"


async def report(state: IncidentState, runtime: Runtime[RunContext]) -> dict:
    world = get_world(world_ref(state))
    brain = _engine(runtime)
    ver = state.get("verification") or {}
    recovered = bool(ver.get("recovered"))
    final = ver.get("status") if recovered else "escalated"
    hyps = state.get("hypotheses") or []
    top = hyps[0] if hyps else {}
    alert_t = world.alert["fired_t"]
    onsets = [_iso_to_t(world, f.get("anomaly_onset")) for f in state.get("findings", [])
              if f["target"] == state["alert"]["service"]]
    onsets = [t for t in onsets if t is not None and t <= alert_t]
    onset_t = min(onsets) if onsets else alert_t - 3
    recovered_t = _recovery_minute(world, state) if recovered else None
    m = {
        "mttd": alert_t - onset_t,
        "mtti": (state.get("diagnosed_t") or world.clock) - alert_t,
        "mttr": (recovered_t - alert_t) if recovered_t is not None else None,
        "duration": ((recovered_t or world.clock) - onset_t),
        "rounds": state.get("round", 0), "findings": len(state.get("findings", [])),
        "tool_calls": sum(f.get("tool_calls", 0) for f in state.get("findings", [])),
    }
    timeline = list(state.get("timeline", []))
    if top.get("change_id"):
        ch = next((c for c in world.changes if c.id == top["change_id"]), None)
        if ch:
            timeline.append(_tl(world, "cause", f"{ch.id}: {ch.summary}", ch.t))
    timeline.append(_tl(world, "onset", f"Anomaly onset on {state['alert']['service']}", onset_t))
    if recovered_t is not None:
        timeline.append(_tl(world, "recovered", "Service metrics back within SLO", recovered_t))
    timeline.sort(key=lambda e: e["t"])
    comp = top.get("component")
    facts = {"engine": brain.label, "team": world.topology.get(comp).team if comp in world.topology else "owners",
             "onset_hhmm": world.hhmm(onset_t), "recovered_hhmm": world.hhmm(recovered_t) if recovered_t else None,
             "now_hhmm": world.hhmm(world.clock), "final_status": final}
    _log("Scribe", "Writing the postmortem and the customer status update.")
    emit("report_start")

    async def on_token(tok: str) -> None:
        emit("report_token", text=tok)

    rep = await brain.report(ReportInput(state={**state, "status": final}, timeline=timeline, metrics=m,
                                         facts=facts), on_token=on_token)
    report_d = {"postmortem": rep.postmortem_markdown, "status_update": rep.status_update, "metrics": m,
                "final_status": final, "timeline": timeline}
    await memory.remember(runtime.store, state["incident_id"], {
        "incident_id": state["incident_id"], "service": state["alert"]["service"], "component": comp,
        "category": top.get("category"), "root_cause": top.get("title"),
        "fix": ", ".join(a["action"] for a in state.get("executed", []) if a.get("ok")) or "none",
        "outcome": final, "closed_at": datetime.now(timezone.utc).isoformat(),
    })
    emit("report", report=report_d)
    _log("Scribe", f"Incident closed as {final.replace('_', ' ').upper()}. Postmortem ready.", level="decision")
    return {"report": report_d, "status": final,
            "timeline": [_tl(world, "closed", f"Incident closed: {final.replace('_', ' ')}")]}


def _iso_to_t(world: World, iso: str | None) -> int | None:
    """ISO timestamp -> simulation minute; None if missing or not a timestamp (LLM output is untrusted)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - world.anchor).total_seconds() // 60)


def _recovery_minute(world: World, state: dict) -> int | None:
    alert = state["alert"]
    first_action = min((a["t"] for a in state.get("executed", []) if a.get("ok")), default=None)
    if first_action is None:
        return None
    pts = world.series(alert["service"], alert["metric"], first_action, world.clock)
    for (t1, v1), (_, v2) in zip(pts, pts[1:]):
        if v1 <= alert["threshold"] and v2 <= alert["threshold"]:
            return t1
    return world.clock


def topology_text(world: World) -> str:
    topo = world.topology
    lines = []
    for c in topo.components.values():
        deps = ", ".join(e.callee for e in topo.callees(c.name)) or "-"
        lines.append(f"- {c.name} [{c.kind}, team {c.team}] -> {deps}")
    return "\n".join(lines)


def _chart_refs(tool: str, art: Any) -> list[dict]:
    """Small series the UI can draw as sparklines inside the activity feed."""
    if not isinstance(art, dict):
        return []
    out = []
    if tool == "get_golden_signals":
        for a in art.get("anomalies", [])[:3]:
            series = art["metrics"].get(a["metric"], {}).get("tail", [])
            out.append({"component": art["component"], "metric": a["metric"], "values": series[-30:]})
    elif tool == "query_metric":
        out.append({"component": art.get("component"), "metric": art.get("metric"),
                    "values": [p[1] for p in art.get("points", [])][-40:]})
    return out


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph(checkpointer=None, store=None):
    g = StateGraph(IncidentState, context_schema=RunContext)
    g.add_node("intake", intake)
    g.add_node("triage", triage, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("dispatch", dispatch)
    g.add_node("investigate", build_investigator())
    g.add_node("synthesize", synthesize, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("critique", critique, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("plan", plan, retry_policy=RetryPolicy(max_attempts=2))
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_node("verify", verify)
    g.add_node("report", report, retry_policy=RetryPolicy(max_attempts=2))

    g.add_edge(START, "intake")
    g.add_edge("intake", "triage")
    g.add_edge("triage", "dispatch")
    g.add_conditional_edges("dispatch", fan_out, ["investigate"])
    g.add_edge("investigate", "synthesize")
    g.add_edge("synthesize", "critique")
    g.add_conditional_edges("critique", after_critique, ["dispatch", "plan"])
    g.add_conditional_edges("plan", after_plan, ["approval", "report"])
    g.add_conditional_edges("approval", after_approval, ["execute", "plan", "report"])
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", after_verify, ["plan", "report"])
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer, store=store, name="argus")


def new_incident_input(scenario: dict, created_at: datetime | None = None, incident_id: str | None = None) -> dict:
    created = (created_at or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    return {"incident_id": incident_id or f"INC-{uuid.uuid4().hex[:6].upper()}", "scenario": scenario,
            "created_at": created.isoformat()}
