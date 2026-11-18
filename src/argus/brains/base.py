"""The reasoning-engine interface.

The graph decides *what happens when*; a ``Brain`` decides *what to conclude*.
Two implementations share this contract:

* :class:`~argus.brains.llm.LLMBrain` -- Claude (or any LangChain chat model)
  with tool calling and structured outputs;
* :class:`~argus.brains.deterministic.DeterministicBrain` -- an offline,
  rule-based reasoner over the same tool outputs.

``ResilientBrain`` runs the LLM first and falls back per call, so a rate limit or
an outage of the model provider degrades the incident response instead of
stopping it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import BaseTool

from ..schemas import Critique, FindingDraft, HypothesisSet, InvestigationTask, PlanDraft, Report, TriageDraft


@dataclass
class TriageInput:
    alert: dict
    signals: dict  # golden-signals artifact of the alerting service
    signals_text: str
    blast_radius: list[str]
    dependencies: list[str]
    memories: list[dict]
    topology_text: str


@dataclass
class SynthesisInput:
    brief: dict
    findings: list[dict]
    memories: list[dict]
    topology_text: str
    previous: list[dict] = field(default_factory=list)


@dataclass
class CritiqueInput:
    brief: dict
    hypotheses: list[dict]
    findings: list[dict]
    task_log: list[dict]
    round: int
    max_rounds: int
    components: list[str]


@dataclass
class PlanInput:
    brief: dict
    hypotheses: list[dict]
    hypothesis: dict
    facts: dict  # component facts: versions, certificates, flags, max_connections, callers ...
    runbooks: list[dict]  # {id, title, content, actions}
    action_catalog: dict
    executed: list[dict]
    feedback: list[str]


@dataclass
class ReportInput:
    state: dict
    timeline: list[dict]
    metrics: dict  # MTTD / MTTR etc.
    facts: dict


TokenCallback = Callable[[str], Awaitable[None]]


class Brain(ABC):
    name: str = "brain"
    label: str = "Brain"

    @abstractmethod
    async def triage(self, inp: TriageInput) -> TriageDraft: ...

    @abstractmethod
    async def next_step(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage],
                        tools: list[BaseTool], step: int) -> AIMessage: ...

    @abstractmethod
    async def summarize(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage]) -> FindingDraft: ...

    @abstractmethod
    async def hypothesize(self, inp: SynthesisInput) -> HypothesisSet: ...

    @abstractmethod
    async def critique(self, inp: CritiqueInput) -> Critique: ...

    @abstractmethod
    async def plan(self, inp: PlanInput) -> PlanDraft: ...

    @abstractmethod
    async def report(self, inp: ReportInput, on_token: TokenCallback | None = None) -> Report: ...


@dataclass
class ToolUse:
    name: str
    args: dict
    artifact: Any
    content: str
    call_id: str


def tool_history(messages: list[AnyMessage]) -> list[ToolUse]:
    """Pair each tool call with its result, in call order."""
    calls: dict[str, tuple[str, dict]] = {}
    order: list[str] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls[tc["id"]] = (tc["name"], tc.get("args") or {})
                order.append(tc["id"])
    results: dict[str, ToolMessage] = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out = []
    for cid in order:
        if cid in results:
            name, args = calls[cid]
            msg = results[cid]
            out.append(ToolUse(name, args, getattr(msg, "artifact", None), str(msg.content), cid))
    return out


def transcript(messages: list[AnyMessage], limit: int = 2400) -> str:
    """Readable transcript of an investigation (for summarisation by a model)."""
    parts = []
    for m in messages:
        if isinstance(m, AIMessage):
            text = m.text if isinstance(m.text, str) else ""
            if text.strip():
                parts.append(f"[investigator] {text.strip()}")
            for tc in m.tool_calls or []:
                parts.append(f"[tool call] {tc['name']}({tc.get('args')})")
        elif isinstance(m, ToolMessage):
            body = str(m.content)
            parts.append(f"[tool result]\n{body[:limit]}" + (" ..." if len(body) > limit else ""))
    return "\n\n".join(parts)


def has_credentials(model: str) -> bool:
    """Is there an API key (or local server) for the configured model provider?"""
    import os

    provider = model.split(":", 1)[0] if ":" in model else "anthropic"
    env = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        "openai": ("OPENAI_API_KEY",),
        "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "azure_openai": ("AZURE_OPENAI_API_KEY",),
        "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"),
        "bedrock_converse": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"),
        "ollama": ("OLLAMA_HOST", "ARGUS_OLLAMA"),
    }.get(provider, ())
    return any(os.environ.get(k) for k in env)
