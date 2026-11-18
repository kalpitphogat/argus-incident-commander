"""Domain models.

These Pydantic models are the contracts between graph nodes *and* the structured
output schemas handed to the LLM.  Graph state stores their JSON (``model_dump``)
so checkpoints stay portable and the web UI can render state directly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Analyst = Literal["metrics", "logs", "changes", "dependencies"]
Category = Literal[
    "bad_deploy", "memory_leak", "resource_exhaustion", "external_dependency", "certificate_expiry",
    "config_change", "traffic_surge", "dependency_failure", "code_exception", "unknown",
]
Risk = Literal["low", "medium", "high"]
ActionName = Literal[
    "restart_service", "scale_service", "rollback_deploy", "increase_connection_pool", "toggle_feature_flag",
    "rotate_certificate", "enable_circuit_breaker",
]
Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4"]

ANALYST_LABEL = {
    "metrics": "Metrics analyst",
    "logs": "Logs analyst",
    "changes": "Change analyst",
    "dependencies": "Dependency analyst",
}

CATEGORY_LABEL = {
    "bad_deploy": "Bad deploy",
    "memory_leak": "Memory leak",
    "resource_exhaustion": "Resource exhaustion",
    "external_dependency": "Third-party outage",
    "certificate_expiry": "Certificate expiry",
    "config_change": "Config change",
    "traffic_surge": "Traffic surge",
    "dependency_failure": "Dependency failure",
    "code_exception": "Code exception",
    "unknown": "Unknown",
}


class Alert(BaseModel):
    id: str
    title: str
    service: str
    metric: str
    value: float
    threshold: float
    rule: str
    fired_at: str


class InvestigationTask(BaseModel):
    """One unit of work for a specialist investigator."""

    analyst: Analyst = Field(description="Which specialist should do this.")
    target: str = Field(description="Exact component name to investigate, e.g. 'orders-db'.")
    objective: str = Field(description="The specific question this task must answer.")
    window_minutes: int = Field(default=60, description="How far back to look (minutes). Use 120-240 for slow, "
                                                         "gradual problems such as leaks.")
    focus_time: str | None = Field(default=None, description="ISO time the anomaly started, if known.")


class Evidence(BaseModel):
    source: Literal["metrics", "logs", "changes", "dependencies", "memory", "runbook"]
    component: str = Field(description="Component the evidence is about.")
    summary: str = Field(description="One factual sentence, with numbers and times.")
    weight: float = Field(default=0.5, description="0-1: how strongly this points at a root cause.")


class SuspectedCause(BaseModel):
    category: Category
    component: str = Field(description="Component where the cause lives (the root, not a symptom).")
    detail: str = Field(description="Short explanation.")
    change_id: str | None = Field(default=None, description="Related change id (CHG-...), if any.")
    weight: float = Field(default=0.5, description="0-1 strength of the evidence for this cause.")


class FindingDraft(BaseModel):
    """What an investigator reports back -- compressed, factual, with evidence."""

    headline: str = Field(description="One line conclusion, e.g. 'orders-db query rate 10x since 13:52'.")
    summary: str = Field(description="2-4 sentences: what was checked, what was found, what it means.")
    evidence: list[Evidence] = Field(description="The concrete observations that support the conclusion.")
    suspected_causes: list[SuspectedCause] = Field(
        default_factory=list, description="Root causes this evidence points to (empty if only symptoms).")
    anomaly_onset: str | None = Field(default=None, description="ISO time the anomaly started, if seen.")
    confidence: float = Field(default=0.5, description="0-1 confidence in the headline.")


class Finding(FindingDraft):
    task_id: str
    analyst: Analyst
    target: str
    round: int
    tool_calls: int = 0
    engine: str = "deterministic"


class Hypothesis(BaseModel):
    id: str = Field(description="H1, H2, ...")
    title: str = Field(description="Specific root-cause statement, e.g. 'checkout-service 2.14.0 introduced an "
                                   "N+1 query against orders-db'.")
    category: Category
    component: str = Field(description="Root-cause component (exact name).")
    change_id: str | None = Field(default=None, description="The change (CHG-...) that triggered it, if any.")
    confidence: float = Field(description="0-1.")
    supporting: list[str] = Field(description="Evidence for, each with its source.")
    contradicting: list[str] = Field(default_factory=list, description="Evidence against or gaps.")
    reasoning: str = Field(description="Why this explains the symptoms (and the blast radius).")


class HypothesisSet(BaseModel):
    hypotheses: list[Hypothesis] = Field(description="Ranked, most likely first. 1-4 entries.")
    summary: str = Field(description="One paragraph for the incident channel.")


class Critique(BaseModel):
    verdict: Literal["sufficient", "needs_more_evidence"]
    reasoning: str = Field(description="Why the evidence is (not) enough to act on the top hypothesis.")
    gaps: list[str] = Field(default_factory=list, description="What is still unknown or unverified.")
    follow_up_tasks: list[InvestigationTask] = Field(
        default_factory=list, description="Targeted tasks that would close the gaps (max 4).")


class ActionParams(BaseModel):
    service: str | None = None
    to_version: str | None = None
    replicas: int | None = None
    datastore: str | None = None
    max_connections: int | None = None
    flag: str | None = None
    enabled: bool | None = None
    certificate: str | None = None
    dependency: str | None = None

    def compact(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class PlannedAction(BaseModel):
    action: ActionName
    params: ActionParams
    rationale: str = Field(description="Why this action, tied to the hypothesis.")
    risk: Risk = Field(description="Blast-radius risk of executing it.")
    expected_effect: str = Field(description="What should happen to the symptoms, and how fast.")


class PlanDraft(BaseModel):
    summary: str = Field(description="One-sentence plan.")
    actions: list[PlannedAction] = Field(description="Ordered actions (most targeted first). 1-3 actions.")
    runbook_ids: list[str] = Field(default_factory=list, description="Runbooks (RB-...) followed.")
    fallback: str = Field(description="What to do if this does not work.")
    customer_message: str = Field(description="A calm, non-technical status-page update.")


class TriageDraft(BaseModel):
    severity: Severity
    title: str = Field(description="Incident title, <= 80 chars.")
    summary: str = Field(description="2-3 sentences: symptom, scope, first read of the situation.")
    customer_impact: str = Field(description="What customers experience right now.")
    suspected_area: str = Field(description="Where the trouble most likely is (component / layer).")
    initial_tasks: list[InvestigationTask] = Field(description="3-4 parallel tasks, ideally one per specialist.")


class Report(BaseModel):
    postmortem_markdown: str
    status_update: str
