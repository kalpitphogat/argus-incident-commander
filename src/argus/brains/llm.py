"""LLM reasoning engine: any LangChain chat model, tuned by default for Claude.

* Investigators are tool-calling agents (``bind_tools``) driven step by step by
  the investigator subgraph, so every tool call streams to the UI.
* Every other decision uses native structured output (``with_structured_output``
  with ``method="json_schema"`` on Anthropic -> ``output_config.format``).
* Effort is set per role: low for the tool-calling sub-agents, high for the
  judgement calls (RCA, critique, planning).
* The postmortem is streamed token by token.

For Anthropic models we also enable server-side refusal fallbacks
(``fallbacks: "default"``) and top-level prompt caching, and never send sampling
parameters (not accepted by current Claude models).
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from ..schemas import ANALYST_LABEL, Critique, FindingDraft, HypothesisSet, InvestigationTask, PlanDraft, Report, \
    TriageDraft
from ..sim.topology import Topology
from . import prompts
from .base import Brain, CritiqueInput, PlanInput, ReportInput, SynthesisInput, TokenCallback, TriageInput, transcript

EFFORT = {"triage": "medium", "investigate": "low", "summarize": "medium", "hypothesize": "high",
          "critique": "high", "plan": "high", "report": "medium"}


def model_label(model: str) -> str:
    name = model.split(":", 1)[-1]
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", name)
    if m:
        family, major, minor = m.groups()
        return f"Claude {family.capitalize()} {major}{'.' + minor if minor else ''}"
    return name


def _topology_text() -> str:
    topo = Topology()
    return "\n".join(f"- {c.name} [{c.kind}, {c.team}] -> {', '.join(e.callee for e in topo.callees(c.name)) or '-'}"
                     for c in topo.components.values())


class LLMBrain(Brain):
    name = "llm"

    def __init__(self, model: str) -> None:
        self.model = model if ":" in model else f"anthropic:{model}"
        self.provider = self.model.split(":", 1)[0]
        self.label = model_label(self.model)
        self._models: dict[str, Any] = {}
        self._topology = _topology_text()

    # -- model handles ----------------------------------------------------------
    def _llm(self, role: str):
        effort = EFFORT.get(role, "medium")
        if effort not in self._models:
            kwargs: dict[str, Any] = {"max_retries": 3, "timeout": 300}
            if self.provider == "anthropic":
                kwargs.update(
                    max_tokens=16000,
                    effort=effort,
                    betas=["server-side-fallback-2026-07-01"],
                    model_kwargs={"fallbacks": "default", "cache_control": {"type": "ephemeral"}},
                )
            self._models[effort] = init_chat_model(self.model, **kwargs)
        return self._models[effort]

    async def _structured(self, role: str, schema: type, system: str, human: str):
        llm = self._llm(role)
        if self.provider == "anthropic":
            runnable = llm.with_structured_output(schema, method="json_schema", include_raw=True)
        else:
            runnable = llm.with_structured_output(schema, include_raw=True)
        out = await runnable.ainvoke([SystemMessage(system), HumanMessage(human)])
        if out.get("parsing_error") or out.get("parsed") is None:
            raise ValueError(f"structured output for {schema.__name__} failed: {out.get('parsing_error')}")
        return out["parsed"]

    # -- roles ----------------------------------------------------------------
    async def triage(self, inp: TriageInput) -> TriageDraft:
        human = (f"ALERT {inp.alert['title']} (rule {inp.alert['rule']}, fired {inp.alert['fired_at']})\n\n"
                 f"Golden signals of {inp.alert['service']}:\n{inp.signals_text}\n\n"
                 f"Dependencies of {inp.alert['service']}: {', '.join(inp.dependencies) or 'none'}\n"
                 f"Blast radius (callers, transitively): {', '.join(inp.blast_radius) or 'none'}\n"
                 f"Similar past incidents: {json.dumps(inp.memories) if inp.memories else 'none'}\n\n"
                 f"Estate:\n{inp.topology_text}\n\n"
                 "Classify severity (SEV1 revenue path down or >20% of a critical flow failing; SEV2 critical flow "
                 "degraded; SEV3 non-critical; SEV4 no customer impact) and plan 3-4 parallel first-round tasks, one "
                 "per specialist (metrics, logs, changes, dependencies), all aimed at the alerting service.")
        return await self._structured("triage", TriageDraft, prompts.TEAM + "\n\nYou are the incident commander "
                                      "doing triage.", human)

    async def next_step(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage],
                        tools: list[BaseTool], step: int) -> AIMessage:
        system = prompts.INVESTIGATOR.format(label=ANALYST_LABEL[task.analyst],
                                             specialty=prompts.SPECIALTY[task.analyst], topology=self._topology)
        focus = f" The anomaly started around {task.focus_time}." if task.focus_time else ""
        human = prompts.INVESTIGATOR_TASK.format(title=brief.get("title", ""), severity=brief.get("severity", ""),
                                                 summary=brief.get("summary", ""), objective=task.objective,
                                                 target=task.target, window=task.window_minutes, focus=focus)
        llm = self._llm("investigate").bind_tools(tools)
        msg = await llm.ainvoke([SystemMessage(system), HumanMessage(human), *messages])
        return msg

    async def summarize(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage]) -> FindingDraft:
        human = (f"Specialist: {ANALYST_LABEL[task.analyst]} (evidence source = '{task.analyst}')\n"
                 f"Task: {task.objective}\nTarget: {task.target}\n\nInvestigation transcript:\n"
                 f"{transcript(messages)}")
        return await self._structured("summarize", FindingDraft, prompts.SUMMARIZE, human)

    async def hypothesize(self, inp: SynthesisInput) -> HypothesisSet:
        findings = [{k: f.get(k) for k in ("round", "analyst", "target", "headline", "summary", "evidence",
                                           "suspected_causes", "anomaly_onset", "confidence")} for f in inp.findings]
        human = (f"Incident brief: {json.dumps(inp.brief)}\n\nFindings so far ({len(findings)}):\n"
                 f"{json.dumps(findings, indent=1)}\n\nPrevious hypotheses: {json.dumps(inp.previous) or 'none'}\n"
                 f"Similar past incidents: {json.dumps(inp.memories) or 'none'}\n\nEstate:\n{inp.topology_text}")
        return await self._structured("hypothesize", HypothesisSet, prompts.SYNTHESIZE, human)

    async def critique(self, inp: CritiqueInput) -> Critique:
        done = [{k: t.get(k) for k in ("round", "analyst", "target", "objective", "window_minutes")}
                for t in inp.task_log]
        findings = [{k: f.get(k) for k in ("round", "analyst", "target", "headline")} for f in inp.findings]
        human = (f"Round {inp.round} of {inp.max_rounds}.\n\nHypotheses:\n{json.dumps(inp.hypotheses, indent=1)}\n\n"
                 f"Findings (headlines):\n{json.dumps(findings, indent=1)}\n\nTasks already done:\n"
                 f"{json.dumps(done, indent=1)}\n\nValid components: {', '.join(inp.components)}\n"
                 f"Specialists: metrics, logs, changes, dependencies.")
        return await self._structured("critique", Critique, prompts.CRITIQUE, human)

    async def plan(self, inp: PlanInput) -> PlanDraft:
        catalog = {k: {"min_risk": v["risk"], "params": v["params"], "description": v["description"]}
                   for k, v in inp.action_catalog.items()}
        books = "\n\n".join(f"### {r['id']}: {r['title']}\nPre-approved actions: {json.dumps(r['actions'])}\n"
                            f"{r['content'][:1800]}" for r in inp.runbooks) or "No runbook matched."
        human = (f"Hypothesis to remediate: {json.dumps(inp.hypothesis)}\n\nOther hypotheses: "
                 f"{json.dumps([h.get('title') for h in inp.hypotheses[1:]])}\n\nFacts: {json.dumps(inp.facts)}\n\n"
                 f"Action catalog: {json.dumps(catalog)}\n\nRunbooks:\n{books}\n\n"
                 f"Already executed: {json.dumps(inp.executed) if inp.executed else 'nothing yet'}\n"
                 f"Feedback (verification / human reviewer): {json.dumps(inp.feedback) if inp.feedback else 'none'}\n"
                 f"Customer impact: {inp.brief.get('customer_impact', '')}")
        return await self._structured("plan", PlanDraft, prompts.PLAN, human)

    async def report(self, inp: ReportInput, on_token: TokenCallback | None = None) -> Report:
        s = inp.state
        facts = {
            "brief": s.get("brief"), "alert": s.get("alert"), "root_cause": (s.get("hypotheses") or [None])[0],
            "other_hypotheses": [h.get("title") for h in (s.get("hypotheses") or [])[1:]],
            "findings": [{k: f.get(k) for k in ("round", "analyst", "target", "headline")} for f in s.get("findings", [])],
            "plan": s.get("plan"), "executed_actions": s.get("executed"), "verifications": s.get("verifications"),
            "timeline": inp.timeline, "metrics_minutes": inp.metrics, "facts": inp.facts,
        }
        llm = self._llm("report")
        chunks: list[str] = []
        async for chunk in llm.astream([SystemMessage(prompts.REPORT), HumanMessage(json.dumps(facts, default=str))]):
            text = chunk.text if isinstance(chunk.text, str) else ""
            if text:
                chunks.append(text)
                if on_token:
                    await on_token(text)
        md = "".join(chunks).strip()
        if not md:
            raise ValueError("empty postmortem")
        status = ""
        m = re.search(r"^##\s*Customer status update\s*$", md, flags=re.M | re.I)
        if m:
            status = md[m.end():].strip()
            md = md[:m.start()].rstrip()
        return Report(postmortem_markdown=md, status_update=status or "We are monitoring the fix.")
