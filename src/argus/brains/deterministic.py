"""An offline, rule-based reasoner.

It drives the same investigation tools as the LLM, reads their structured
artifacts and applies the heuristics an experienced SRE uses: follow the hot
path, look for new log signatures, correlate changes with the onset, require
corroboration from independent sources, and prefer the runbook's pre-approved
actions.  It never looks at the injected fault -- only at telemetry.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.tools import BaseTool

from ..analysis import correlate_change, extract_refs
from ..events import pace
from ..schemas import (
    CATEGORY_LABEL, ActionParams, Critique, Evidence, FindingDraft, Hypothesis, HypothesisSet, InvestigationTask,
    PlanDraft, PlannedAction, Report, SuspectedCause, TriageDraft,
)
from ..sim.topology import Topology
from ..sim.world import ACTION_CATALOG, RISK_ORDER
from .base import Brain, CritiqueInput, PlanInput, ReportInput, SynthesisInput, TokenCallback, ToolUse, \
    TriageInput, tool_history

JOURNEY = {
    "web-frontend": "Customers see slow or failing pages across the storefront.",
    "api-gateway": "A share of customer API calls fail or time out across the site.",
    "auth-service": "Customers cannot log in or refresh their session.",
    "catalog-service": "Product pages load slowly or fail.",
    "search-service": "Search is slow or returns errors.",
    "recommendation-service": "Homepage and product recommendations fail intermittently.",
    "cart-service": "Adding items to the cart fails for some customers.",
    "checkout-service": "Customers placing orders see slow or failing checkouts.",
    "inventory-service": "Stock checks and reservations fail, so some checkouts fail.",
    "payment-service": "Card payments fail at checkout.",
    "notification-service": "Order confirmation emails are delayed.",
}

GRADUAL = ("ramp", "sawtooth")
SPECIFIC = ("bad_deploy", "memory_leak", "resource_exhaustion", "external_dependency", "certificate_expiry",
            "config_change", "code_exception")


def _iso_minutes(a: str, b: str) -> float:
    """Minutes from ISO time `b` to ISO time `a` (a - b)."""
    da = datetime.fromisoformat(a.replace("Z", "+00:00"))
    db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (da - db).total_seconds() / 60


def _hhmm(iso: str | None) -> str:
    return iso[11:16] if iso else "?"


def _call(name: str, args: dict, thought: str, task_key: str, step: int) -> AIMessage:
    return AIMessage(content=thought, tool_calls=[{"name": name, "args": args, "id": f"call_{task_key}_{step}",
                                                   "type": "tool_call"}])


@dataclass
class _Cand:
    category: str
    component: str
    sources: dict = field(default_factory=dict)  # source -> (weight, text)
    change_ids: list = field(default_factory=list)
    details: list = field(default_factory=list)
    contradicting: list = field(default_factory=list)
    links: set = field(default_factory=set)  # components this hypothesis is explicitly linked to

    def add(self, source: str, weight: float, text: str) -> None:
        cur = self.sources.get(source)
        if cur is None or weight > cur[0]:
            self.sources[source] = (weight, text)

    def absorb(self, other: "_Cand") -> None:
        for src, (w, text) in other.sources.items():
            self.add(src, w, text)
        for cid in other.change_ids:
            if cid not in self.change_ids:
                self.change_ids.append(cid)
        self.details += [d for d in other.details if d not in self.details]
        self.contradicting += [c for c in other.contradicting if c not in self.contradicting]
        self.links |= other.links

    @property
    def score(self) -> float:
        return sum(w for w, _ in self.sources.values())

    @property
    def confidence(self) -> float:
        conf = 1 - math.exp(-1.15 * self.score)
        if self.contradicting:
            conf *= 0.55
        return round(min(conf, 0.99), 2)


class DeterministicBrain(Brain):
    name = "deterministic"
    label = "Deterministic reasoner (offline)"

    def __init__(self, topology: Topology | None = None, pace_seconds: float = 0.0) -> None:
        self.topo = topology or Topology()
        self.pace_seconds = pace_seconds

    # ------------------------------------------------------------------ triage
    async def triage(self, inp: TriageInput) -> TriageDraft:
        await pace(self.pace_seconds)
        alert = inp.alert
        svc = alert["service"]
        comp = self.topo.get(svc)
        metrics = inp.signals["metrics"]
        err = max(metrics.get("error_rate_pct", {}).get("current") or 0.0,
                  alert["value"] if alert["metric"] == "error_rate_pct" else 0.0)
        lat = max(metrics.get("latency_p99_ms", {}).get("current") or 0.0,
                  alert["value"] if alert["metric"] == "latency_p99_ms" else 0.0)
        lat_ratio = lat / comp.slo_latency_ms if comp.slo_latency_ms else 0.0
        critical = comp.revenue_critical
        if err >= 50 or (critical and (err >= 20 or lat_ratio >= 4)):
            sev = "SEV1"
        elif critical and (err >= 2 or lat_ratio >= 1.5):
            sev = "SEV2"
        elif err > (comp.slo_error_pct or 1) or lat_ratio > 1:
            sev = "SEV3"
        else:
            sev = "SEV4"
        anomalies = inp.signals["anomalies"]
        onset = anomalies[0]["onset"] if anomalies else alert["fired_at"]
        symptom = (f"p99 latency {lat:,.0f} ms (SLO {comp.slo_latency_ms:g} ms)" if alert["metric"] == "latency_p99_ms"
                   else f"error rate {err:.1f}% (SLO {comp.slo_error_pct:g}%)")
        anomaly_txt = "; ".join(a["text"] for a in anomalies[:3]) or "no other anomalies yet"
        edge = [c for c in inp.blast_radius if self.topo.get(c).tier == "edge"]
        summary = (f"{svc} {symptom} since about {_hhmm(onset)} UTC. {anomaly_txt}. Blast radius: "
                   f"{len(inp.blast_radius)} upstream component(s)" + (f" including {', '.join(edge)}" if edge else "")
                   + ".")
        if inp.memories:
            m = inp.memories[0]
            summary += f" Similar past incident {m.get('incident_id')}: {m.get('root_cause')}."
        deps = ", ".join(inp.dependencies) or "none"
        window = 60
        tasks = [
            InvestigationTask(analyst="metrics", target=svc, window_minutes=window, focus_time=onset,
                              objective=f"Characterise the anomaly on {svc}: which golden signals moved, when, and "
                                        f"with what shape?"),
            InvestigationTask(analyst="logs", target=svc, window_minutes=window, focus_time=onset,
                              objective=f"Find new or surging error patterns in {svc} logs since the onset."),
            InvestigationTask(analyst="changes", target=svc, window_minutes=120, focus_time=onset,
                              objective=f"Which deploys, flags, config or certificate changes line up with the onset "
                                        f"at {_hhmm(onset)}?"),
            InvestigationTask(analyst="dependencies", target=svc, window_minutes=15, focus_time=onset,
                              objective=f"Follow the hot path from {svc} (depends on: {deps}): which hop explains the "
                                        f"extra latency / errors?"),
        ]
        return TriageDraft(
            severity=sev, title=f"{svc}: {symptom}"[:80], summary=summary,
            customer_impact=JOURNEY.get(svc, f"Requests served by {svc} are degraded."),
            suspected_area=f"{svc} or one of its dependencies ({deps})", initial_tasks=tasks)

    # ---------------------------------------------------- investigator: steps
    async def next_step(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage],
                        tools: list[BaseTool], step: int) -> AIMessage:
        await pace(self.pace_seconds * 0.8)
        key = f"{task.analyst}_{abs(hash((task.target, task.objective))) % 10**6}"
        hist = tool_history(messages)
        names = [h.name for h in hist]
        tgt = task.target
        if tgt not in self.topo:
            return AIMessage(content=f"'{tgt}' is not a known component; nothing to investigate.")
        comp = self.topo.get(tgt)

        def call(name: str, args: dict, thought: str) -> AIMessage:
            return _call(name, args, thought, key, step)

        def last(name: str) -> ToolUse | None:
            return next((h for h in reversed(hist) if h.name == name), None)

        done = AIMessage(content="I have enough to report back.")
        if task.analyst == "metrics":
            if "get_golden_signals" not in names:
                return call("get_golden_signals", {"component": tgt, "window_minutes": max(60, task.window_minutes)},
                            f"Pulling golden signals for {tgt} and running anomaly detection against its baseline.")
            sig = last("get_golden_signals")
            art = sig.artifact if sig else None
            if "query_metric" not in names and isinstance(art, dict) and art.get("anomalies") is not None:
                metrics = {a["metric"] for a in art["anomalies"]}
                if metrics & {"memory_pct", "restarts"}:
                    return call("query_metric", {"component": tgt, "metric": "memory_pct", "window_minutes": 120},
                                "Memory looks abnormal - pulling the 2-hour memory curve to check for a leak "
                                "(linear growth followed by resets).")
                if "connections" in metrics:
                    return call("query_metric", {"component": tgt, "metric": "connections", "window_minutes": 45},
                                "The connection count moved - comparing it with max_connections.")
            return done
        if task.analyst == "logs":
            if "get_log_patterns" not in names:
                return call("get_log_patterns", {"component": tgt, "window_minutes": max(45, task.window_minutes)},
                            f"Clustering {tgt} logs into templates and diffing before vs after the onset.")
            pat = last("get_log_patterns")
            if "search_logs" not in names and isinstance(pat.artifact, dict):
                hot = [t for t in pat.artifact.get("templates", []) if t["status"] in ("new", "surging")
                       and t["level"] in ("WARN", "ERROR", "FATAL", "LOG") and t.get("signature")]
                if hot:
                    phrase = _search_phrase(hot[0]["sample"])
                    return call("search_logs", {"component": tgt, "query": phrase, "level": "INFO",
                                                "window_minutes": 20, "limit": 8},
                                f"Pulling raw lines for the strongest new pattern ('{phrase}').")
            return done
        if task.analyst == "changes":
            if "list_recent_changes" not in names:
                return call("list_recent_changes", {"window_minutes": max(90, task.window_minutes)},
                            "Listing every change in the window to correlate with the onset.")
            if "list_certificates" not in names:
                return call("list_certificates", {"component": tgt},
                            f"Checking certificates used by {tgt} (expiries line up to the minute).")
            downstream = set(self.topo.downstream(tgt)) | {tgt}
            if "check_vendor_status" not in names and any(self.topo.get(c).kind == "external" for c in downstream):
                return call("check_vendor_status", {}, "A third-party API is on this path - checking vendor status.")
            changes = (last("list_recent_changes").artifact or {}).get("changes", [])
            deployed = [c for c in changes if c["kind"] == "deploy" and c["component"] in downstream]
            if deployed and "get_deploy_history" not in names:
                return call("get_deploy_history", {"service": deployed[0]["component"]},
                            f"{deployed[0]['component']} was deployed recently - getting its version history.")
            return done
        if task.analyst == "dependencies":
            deps = [h for h in hist if h.name == "get_service_dependencies"]
            traces = [h for h in hist if h.name == "get_trace_breakdown"]
            if not deps:
                return call("get_service_dependencies", {"component": tgt},
                            f"Mapping {tgt}'s dependencies and their current health.")
            if not traces and comp.kind == "service":
                return call("get_trace_breakdown", {"component": tgt, "window_minutes": 10},
                            f"Breaking {tgt}'s request time and errors down per call hop.")
            if traces and len(traces) < 4:
                path = _hot_path(self.topo, tgt, deps, traces)
                nxt = path[-1] if path else None
                if nxt and nxt["kind"] == "service" and nxt["anomalous"] and nxt["name"] not in \
                        [h.args.get("component") for h in traces]:
                    if nxt["name"] not in [h.args.get("component") for h in deps]:
                        return call("get_service_dependencies", {"component": nxt["name"]},
                                    f"The hot path leads to {nxt['name']} - checking its dependencies.")
                    return call("get_trace_breakdown", {"component": nxt["name"], "window_minutes": 10},
                                f"Following the hot path into {nxt['name']}.")
                if nxt and nxt["kind"] == "external" and "check_vendor_status" not in names:
                    return call("check_vendor_status", {},
                                f"The hot path ends at third-party {nxt['name']} - checking the vendor's status page.")
            return done
        return done

    # ------------------------------------------------- investigator: summaries
    async def summarize(self, task: InvestigationTask, brief: dict, messages: list[AnyMessage]) -> FindingDraft:
        await pace(self.pace_seconds * 0.5)
        hist = tool_history(messages)
        fn = {"metrics": self._sum_metrics, "logs": self._sum_logs, "changes": self._sum_changes,
              "dependencies": self._sum_dependencies}[task.analyst]
        try:
            return fn(task, hist)
        except Exception as exc:  # defensive: a malformed artifact must not kill the incident
            return FindingDraft(headline=f"Could not analyse {task.target}: {exc}", summary=str(exc), evidence=[],
                                confidence=0.1)

    def _sum_metrics(self, task: InvestigationTask, hist: list[ToolUse]) -> FindingDraft:
        sig = next((h.artifact for h in reversed(hist) if h.name == "get_golden_signals"), None)
        tgt = task.target
        if not isinstance(sig, dict) or "metrics" not in sig:
            return FindingDraft(headline=f"No metrics available for {tgt}", summary="The metrics query failed.",
                                evidence=[], confidence=0.1)
        comp = self.topo.get(tgt)
        anomalies = sig["anomalies"]
        ev: list[Evidence] = []
        causes: list[SuspectedCause] = []
        by_metric = {a["metric"]: a for a in anomalies}
        for a in anomalies:
            w = 0.3
            if a["metric"] == "memory_pct" and a["pattern"] in GRADUAL:
                w = 0.8
            elif a["metric"] == "restarts":
                w = 0.7
            elif a["metric"] == "connections" and a["direction"] == "up":
                w = 0.5
            elif a["metric"].startswith("throughput") and a["direction"] == "up":
                w = 0.45
            ev.append(Evidence(source="metrics", component=tgt, summary=a["text"], weight=w))
        if ("memory_pct" in by_metric and by_metric["memory_pct"]["pattern"] in GRADUAL) or "restarts" in by_metric:
            mem = by_metric.get("memory_pct")
            causes.append(SuspectedCause(
                category="memory_leak", component=tgt, weight=0.8,
                detail=f"memory climbs linearly then resets (OOMKilled restarts) since "
                       f"{_hhmm(mem['onset']) if mem else '?'}"))
        conn = sig["metrics"].get("connections")
        if conn and conn.get("max"):
            peak = max(conn.get("tail") or [0])
            util = peak / conn["max"]
            if util >= 0.95:
                ev.append(Evidence(source="metrics", component=tgt, weight=0.85,
                                   summary=f"connections at {peak:.0f} / max_connections {conn['max']} "
                                           f"({util:.0%}) - the pool is saturated"))
                causes.append(SuspectedCause(category="resource_exhaustion", component=tgt, weight=0.85,
                                             detail=f"connections pinned at max_connections ({conn['max']})"))
            elif conn.get("anomaly") and conn["anomaly"]["direction"] == "down":
                ev.append(Evidence(source="metrics", component=tgt, weight=0.2,
                                   summary=f"connections dropped {conn['anomaly']['change_pct']:.0f}% - clients are "
                                           f"failing to connect, the datastore itself is not overloaded"))
        tp = next((a for a in anomalies if a["metric"].startswith("throughput") and a["direction"] == "up"), None)
        if tp and comp.kind == "datastore" and tp["change_pct"] >= 300:
            causes.append(SuspectedCause(category="dependency_failure", component=tgt, weight=0.35,
                                         detail=f"load-driven: query volume {tp['change_pct'] / 100 + 1:.0f}x baseline"))
        if tp and comp.kind == "service" and tp["change_pct"] >= 60:
            causes.append(SuspectedCause(category="traffic_surge", component=tgt, weight=0.45,
                                         detail=f"traffic {tp['change_pct']:+.0f}% since {_hhmm(tp['onset'])}"))
        if comp.kind == "external" and any(a["metric"] in ("error_rate_pct", "latency_p99_ms") for a in anomalies):
            causes.append(SuspectedCause(category="external_dependency", component=tgt, weight=0.7,
                                         detail="third-party API errors and latency jumped together"))
        sym = [a for a in anomalies if a["metric"] in ("error_rate_pct", "latency_p99_ms", "query_latency_p95_ms")]
        if not anomalies:
            ev.append(Evidence(source="metrics", component=tgt, weight=0.0,
                               summary=f"{tgt}: all golden signals within normal range (health "
                                       f"{sig['health']})"))
            headline = f"{tgt} looks healthy - no anomalous metrics"
        else:
            lead = next((a for a in anomalies if a["metric"] in ("memory_pct", "connections")), None) or \
                (sym[0] if sym else anomalies[0])
            headline = f"{tgt}: {lead['text']}"
        onset = anomalies[0]["onset"] if anomalies else None
        gradual = [a for a in anomalies if a["pattern"] in GRADUAL]
        summary = (f"Checked {len(sig['metrics'])} golden signals on {tgt} (health {sig['health']}). "
                   + (f"{len(anomalies)} anomalous: " + "; ".join(a["text"] for a in anomalies[:4]) + ". "
                      if anomalies else "Nothing anomalous. ")
                   + ("The shape is gradual (ramp / saw-tooth), so the trigger may be well before the alert."
                      if gradual else ""))
        return FindingDraft(headline=headline, summary=summary.strip(), evidence=ev, suspected_causes=causes,
                            anomaly_onset=(gradual[0]["onset"] if gradual else onset),
                            confidence=0.85 if anomalies else 0.7)

    def _sum_logs(self, task: InvestigationTask, hist: list[ToolUse]) -> FindingDraft:
        pat = next((h.artifact for h in reversed(hist) if h.name == "get_log_patterns"), None)
        tgt = task.target
        if not isinstance(pat, dict) or "templates" not in pat:
            return FindingDraft(headline=f"No logs available for {tgt}", summary="The log query failed.", evidence=[],
                                confidence=0.1)
        comp = self.topo.get(tgt)
        hot = [t for t in pat["templates"] if t["status"] in ("new", "surging")]
        signed = [t for t in hot if t.get("signature")]
        ev: list[Evidence] = []
        causes: list[SuspectedCause] = []
        for t in signed[:6]:
            refs = extract_refs(t["sample"])
            sig = t["signature"]
            count = t["after"]
            text = (f"{t['status'].upper()} {t['level']} pattern x{count} since {_hhmm(t['first_seen'])}: "
                    f"{t['sample'][:160]}")
            w = 0.5
            cause = None
            if sig == "certificate_expiry":
                owner = refs.get("component") if comp.kind == "datastore" and "client=" in t["sample"] else tgt
                if comp.kind == "datastore":
                    owner = refs.get("component", tgt)
                cause = SuspectedCause(category="certificate_expiry", component=owner, weight=0.9,
                                       detail=f"TLS handshake failures: certificate expired ({t['sample'][:90]})")
                w = 0.9
            elif sig == "resource_exhaustion":
                where = refs.get("component", tgt) if comp.kind != "datastore" else tgt
                cause = SuspectedCause(category="resource_exhaustion", component=where, weight=0.85,
                                       detail=f"connection pool exhausted on {where}")
                w = 0.85
            elif sig == "memory_leak":
                strong = "OutOfMemory" in t["sample"] or "OOMKilled" in t["sample"]
                cause = SuspectedCause(category="memory_leak", component=tgt, weight=0.85 if strong else 0.6,
                                       detail="OOM / GC thrash in the service's own logs")
                w = cause.weight
            elif sig == "external_dependency":
                where = refs.get("vendor") or next((e.callee for e in self.topo.callees(tgt)
                                                    if self.topo.get(e.callee).kind == "external"), tgt)
                cause = SuspectedCause(category="external_dependency", component=where, weight=0.85,
                                       detail=f"calls to third-party {where} failing (503 / timeouts)")
                w = 0.85
            elif sig == "query_regression":
                if refs.get("client") and refs.get("version"):
                    cause = SuspectedCause(category="bad_deploy", component=refs["client"], weight=0.75,
                                           detail=f"new query pattern from {refs['client']}@{refs['version']} "
                                                  f"against {tgt}")
                    w = 0.75
                elif refs.get("component") and refs["component"] in self.topo:
                    cause = SuspectedCause(category="dependency_failure", component=refs["component"], weight=0.45,
                                           detail=f"slow calls from {tgt} to {refs['component']}")
                    w = 0.45
            elif sig == "code_exception":
                if refs.get("build"):
                    cause = SuspectedCause(category="bad_deploy", component=tgt, weight=0.7,
                                           detail=f"new exception in build {refs['build']} of {tgt}")
                else:
                    cause = SuspectedCause(category="code_exception", component=tgt, weight=0.6,
                                           detail="new unhandled exception")
                w = cause.weight
            elif sig == "dependency_failure":
                dep = refs.get("component")
                if dep and dep in self.topo and dep != tgt:
                    cause = SuspectedCause(category="dependency_failure", component=dep, weight=0.45,
                                           detail=f"{tgt} sees timeouts / 5xx from {dep}")
                w = 0.45
            elif sig == "traffic_surge":
                cause = SuspectedCause(category="traffic_surge", component=tgt, weight=0.5,
                                       detail=f"edge traffic surge: {t['sample'][:80]}")
            ev.append(Evidence(source="logs", component=tgt, summary=text, weight=w))
            if cause and not any(c.category == cause.category and c.component == cause.component for c in causes):
                causes.append(cause)
        unsigned_errors = [t for t in hot if not t.get("signature") and t["level"] in ("ERROR", "FATAL")]
        for t in unsigned_errors[:2]:
            ev.append(Evidence(source="logs", component=tgt, weight=0.2,
                               summary=f"{t['status'].upper()} {t['level']} x{t['after']}: {t['sample'][:140]}"))
        if signed:
            lead = signed[0]
            headline = f"{tgt}: {lead['signature_label']} - {lead['status']} pattern x{lead['after']} since " \
                       f"{_hhmm(lead['first_seen'])}"
        elif hot:
            headline = f"{tgt}: {len(hot)} new/surging patterns, no known failure signature"
        else:
            headline = f"{tgt}: no new error patterns"
        summary = (f"Mined {pat.get('total_lines', 0)} log lines from {tgt} into {len(pat['templates'])} templates "
                   f"(split at {_hhmm(pat.get('split'))}). {len(hot)} new or surging. "
                   + (" ".join(f"[{t['signature_label']}] {t['template'][:110]}." for t in signed[:3])
                      if signed else ""))
        if pat.get("note"):
            summary = pat["note"] + " " + summary
        onset = min((t["first_seen"] for t in signed), default=None)
        return FindingDraft(headline=headline, summary=summary.strip(), evidence=ev, suspected_causes=causes,
                            anomaly_onset=onset, confidence=0.85 if signed else 0.5)

    def _sum_changes(self, task: InvestigationTask, hist: list[ToolUse]) -> FindingDraft:
        art = next((h.artifact for h in reversed(hist) if h.name == "list_recent_changes"), None) or {}
        certs = next((h.artifact for h in reversed(hist) if h.name == "list_certificates"), None) or {}
        vendors = next((h.artifact for h in reversed(hist) if h.name == "check_vendor_status"), None) or {}
        tgt = task.target
        changes = art.get("changes", [])
        now_t, now_iso = art.get("now_t"), art.get("now")
        focus_t = art.get("alert_t", 0) - 3
        if task.focus_time and now_iso and now_t is not None:
            try:
                focus_t = now_t + round(_iso_minutes(task.focus_time, now_iso))
            except (ValueError, TypeError):
                pass  # not a timestamp (free text from a model): keep the alert-based default
        gradual = task.window_minutes >= 150 or any(w in task.objective.lower() for w in ("gradual", "leak", "ramp"))
        focus = {tgt}
        related = set(self.topo.downstream(tgt)) | {e.caller for e in self.topo.callers(tgt)}
        scored = []
        for c in changes:
            ch = type("C", (), {"t": c["t"], "kind": c["kind"], "component": c["component"],
                                "details": c.get("details") or {}})()
            score, why = correlate_change(ch, focus_t, focus, related, gradual)
            scored.append((score, why, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        ev: list[Evidence] = []
        causes: list[SuspectedCause] = []
        for score, why, c in scored[:4]:
            if score < 0.05:
                continue
            ev.append(Evidence(source="changes", component=c["component"], weight=round(min(0.9, score), 2),
                               summary=f"{c['id']} {_hhmm(c['time'])} [{c['kind']}] {c['summary']} - {why} "
                                       f"(correlation {score:.2f})"))
            if score < 0.3:
                continue
            if c["kind"] == "deploy":
                d = c.get("details") or {}
                causes.append(SuspectedCause(
                    category="bad_deploy", component=c["component"], change_id=c["id"], weight=round(min(0.9, score), 2),
                    detail=f"{c['component']} {d.get('from_version')} -> {d.get('to_version')} deployed "
                           f"{_hhmm(c['time'])} ({c['id']}), {why}"))
            elif c["kind"] == "feature_flag":
                flag = (c.get("details") or {}).get("flag")
                causes.append(SuspectedCause(
                    category="traffic_surge", component=c["component"], change_id=c["id"],
                    weight=round(min(0.85, score), 2),
                    detail=f"feature flag '{flag}' changed {_hhmm(c['time'])} ({c['id']}), {why}"))
            elif c["kind"] == "config":
                causes.append(SuspectedCause(category="config_change", component=c["component"], change_id=c["id"],
                                             weight=round(min(0.7, score), 2),
                                             detail=f"{c['summary']} ({c['id']}), {why}"))
        for cert in certs.get("certificates", []):
            hours = cert.get("expires_in_hours", 999)
            if cert.get("expired") or hours < 0.1:
                ev.append(Evidence(source="changes", component=cert["owner"], weight=0.9,
                                   summary=f"certificate {cert['name']} ({cert['used_for']}) expired at "
                                           f"{_hhmm(cert['not_after'])}"))
                causes.append(SuspectedCause(category="certificate_expiry", component=cert["owner"], weight=0.9,
                                             detail=f"certificate {cert['name']} expired at {cert['not_after']}"))
        for v in vendors.get("vendors", []):
            if v["status"] != "operational":
                comp = v["component"] if v["component"] in self.topo else "payrail-api"
                ev.append(Evidence(source="changes", component=comp, weight=0.85,
                                   summary=f"{v['vendor']} status page: {v['status']} since {_hhmm(v.get('since'))} - "
                                           f"{v['title']}"))
                causes.append(SuspectedCause(category="external_dependency", component=comp, weight=0.85,
                                             detail=f"{v['vendor']} reports {v['status']}: {v['title']}"))
        if causes:
            top = max(causes, key=lambda c: c.weight)
            headline = top.detail[:120]
        elif changes:
            headline = f"No change lines up with the onset at {_hhmm(task.focus_time)} " \
                       f"({len(changes)} unrelated changes in the window)"
        else:
            headline = "No changes in the window"
        summary = (f"Reviewed {len(changes)} changes in the last {art.get('window_minutes', '?')} min against the "
                   f"onset at {_hhmm(task.focus_time)}"
                   + (" (wide window: gradual failure)" if gradual else "") + ". "
                   + ("; ".join(e.summary for e in ev[:3]) if ev else "Nothing correlates."))
        return FindingDraft(headline=headline, summary=summary, evidence=ev, suspected_causes=causes,
                            confidence=0.8 if causes else 0.6)

    def _sum_dependencies(self, task: InvestigationTask, hist: list[ToolUse]) -> FindingDraft:
        deps = [h for h in hist if h.name == "get_service_dependencies"]
        traces = [h for h in hist if h.name == "get_trace_breakdown"]
        vendors = next((h.artifact for h in reversed(hist) if h.name == "check_vendor_status"), None) or {}
        tgt = task.target
        path = _hot_path(self.topo, tgt, deps, traces)
        ev: list[Evidence] = []
        causes: list[SuspectedCause] = []
        chain = [tgt] + [p["name"] for p in path]
        for p in path:
            ev.append(Evidence(source="dependencies", component=p["name"], weight=0.5,
                               summary=f"hop {p['from']} -> {p['name']}: {p['delta_ms']:+.0f} ms, "
                                       f"{p['delta_err']:+.2f} pts errors; {p['name']} health {p['health']}"
                                       + (f" ({'; '.join(p['anomalies'][:2])})" if p["anomalies"] else "")))
        onset = None
        if not path:
            own = deps[0].artifact if deps and isinstance(deps[0].artifact, dict) else {}
            self_hot = _self_hot(traces[0].artifact if traces else None)
            if self_hot:
                headline = f"Errors/latency originate inside {tgt} itself (no dependency explains them)"
                causes.append(SuspectedCause(category="code_exception", component=tgt, weight=0.5,
                                             detail=f"trace self-time/errors grew on {tgt}: {self_hot}"))
                ev.append(Evidence(source="dependencies", component=tgt, weight=0.5, summary=headline))
            else:
                headline = f"No dependency of {tgt} shows a significant change"
                ev.append(Evidence(source="dependencies", component=tgt, weight=0.1,
                                   summary=f"{tgt} health {own.get('health', '?')}; dependencies look normal"))
        else:
            origin = path[-1]
            onset = origin.get("onset")
            gradual = origin.get("gradual")
            if origin["kind"] == "external":
                causes.append(SuspectedCause(category="external_dependency", component=origin["name"], weight=0.75,
                                             detail=f"hot path {' -> '.join(chain)} ends at a third-party API"))
                headline = f"Hot path {' -> '.join(chain)}: third-party {origin['name']} is failing"
            elif not origin["anomalous"]:
                client = origin["from"]
                causes.append(SuspectedCause(
                    category="dependency_failure", component=client, weight=0.4,
                    detail=f"calls {client} -> {origin['name']} fail while {origin['name']} itself is healthy: "
                           f"client-side problem (credentials, TLS, pool config)"))
                headline = (f"Calls {client} -> {origin['name']} fail, but {origin['name']} is healthy - the problem "
                            f"is on {client}'s side of the connection")
                ev.append(Evidence(source="dependencies", component=origin["name"], weight=0.4, summary=headline))
            else:
                detail = "; ".join(origin["anomalies"][:3])
                causes.append(SuspectedCause(category="dependency_failure", component=origin["name"], weight=0.6,
                                             detail=f"origin of the hot path {' -> '.join(chain)}: {detail}"))
                headline = f"Hot path {' -> '.join(chain)}: {origin['name']} is the deepest anomalous hop"
                if gradual:
                    headline += " (gradual pattern)"
        for v in vendors.get("vendors", []):
            if v["status"] != "operational":
                comp = v["component"] if v["component"] in self.topo else "payrail-api"
                ev.append(Evidence(source="dependencies", component=comp, weight=0.8,
                                   summary=f"{v['vendor']} status: {v['status']} - {v['title']}"))
                for c in causes:
                    if c.component == comp:
                        c.weight = max(c.weight, 0.8)
        summary = (f"Mapped {tgt}'s dependencies and followed the hot path through traces: "
                   f"{' -> '.join(chain)}. " + " ".join(e.summary for e in ev[:3]))
        return FindingDraft(headline=headline, summary=summary, evidence=ev, suspected_causes=causes,
                            anomaly_onset=onset, confidence=0.75 if path else 0.55)

    # ---------------------------------------------------------- hypotheses
    async def hypothesize(self, inp: SynthesisInput) -> HypothesisSet:
        await pace(self.pace_seconds)
        topo = self.topo
        cands: dict[tuple[str, str], _Cand] = {}

        def cand(category: str, component: str) -> _Cand:
            key = (category, component)
            if key not in cands:
                cands[key] = _Cand(category, component)
            return cands[key]

        healthy_examined: dict[str, str] = {}
        symptoms: dict[str, tuple[float, str]] = {}
        for f in inp.findings:
            src = f["analyst"]
            for c in f.get("suspected_causes", []):
                if c["component"] not in topo:
                    continue
                h = cand(c["category"], c["component"])
                h.add(src, float(c.get("weight", 0.5)), f"[{src}] {c['detail']}")
                if c.get("change_id") and c["change_id"] not in h.change_ids:
                    h.change_ids.append(c["change_id"])
                h.details.append(c["detail"])
                if c["category"] == "bad_deploy" and " against " in c["detail"]:
                    h.links.add(c["detail"].rsplit(" against ", 1)[1].strip())
            if src == "metrics":
                anomalous = [e for e in f.get("evidence", []) if e["weight"] > 0]
                if not anomalous:
                    healthy_examined[f["target"]] = f["headline"]
                elif any(e["weight"] >= 0.3 for e in anomalous):
                    symptoms[f["target"]] = (0.3, f"[metrics] {f['headline']}")

        # traffic surge from a feature flag + exhausted resource downstream of it -> one hypothesis
        for (cat, comp), h in list(cands.items()):
            if cat != "resource_exhaustion":
                continue
            callers = {e.caller for e in topo.callers(comp)}
            for (cat2, comp2), h2 in list(cands.items()):
                if cat2 == "traffic_surge" and (comp2 in callers or comp2 in topo.upstream(comp) or h2.change_ids):
                    h.absorb(h2)
                    cands.pop((cat2, comp2), None)
        # a deploy of S that also shows a leak / exception on S -> one hypothesis
        for (cat, comp), h in list(cands.items()):
            if cat == "bad_deploy" and ("memory_leak", comp) in cands:
                cands[("memory_leak", comp)].absorb(h)
                cands.pop((cat, comp))
            elif cat == "code_exception" and ("bad_deploy", comp) in cands:
                cands[("bad_deploy", comp)].absorb(h)
                cands.pop((cat, comp))
            elif cat == "config_change" and ("resource_exhaustion", comp) in cands:
                cands[("resource_exhaustion", comp)].absorb(h)
                cands.pop((cat, comp))
        # generic "X is failing" evidence joins the specific explanation it belongs to
        for (cat, comp), h in list(cands.items()):
            if cat != "dependency_failure":
                continue
            target = next((cands[(c2, comp)] for c2 in SPECIFIC if (c2, comp) in cands), None)
            if target is None:
                # a failing X explained by a specific root further down X's call graph
                below = set(topo.downstream(comp))
                target = next((h2 for (c2, comp2), h2 in cands.items() if c2 in SPECIFIC and comp2 in below), None)
            if target is None:
                # a datastore overloaded by a client whose new version is explicitly linked to it
                target = next((h2 for (c2, comp2), h2 in cands.items() if c2 == "bad_deploy" and comp in h2.links),
                              None)
            if target is not None:
                target.absorb(h)
                cands.pop((cat, comp))
        for (cat, comp), h in cands.items():
            if comp in symptoms and cat != "external_dependency":
                h.add("metrics", *symptoms[comp])
            if comp in healthy_examined and cat in ("dependency_failure", "resource_exhaustion", "memory_leak",
                                                     "external_dependency"):
                h.contradicting.append(f"[metrics] {healthy_examined[comp]}")
            if cat == "traffic_surge" and not any(c2 == "resource_exhaustion" for c2, _ in cands):
                h.contradicting.append("a surge alone does not explain errors unless something saturates")
        for m in inp.memories:
            key = (m.get("category"), m.get("component"))
            if key in cands:
                cands[key].add("memory", 0.15, f"[memory] matches past incident {m.get('incident_id')}")

        ranked = sorted(cands.values(), key=lambda h: (h.confidence, h.score), reverse=True)[:4]
        hyps = []
        for i, h in enumerate(ranked, 1):
            supporting = [text for _, text in sorted(h.sources.values(), key=lambda x: x[0], reverse=True)]
            hyps.append(Hypothesis(
                id=f"H{i}", title=_title(h), category=h.category, component=h.component,
                change_id=h.change_ids[0] if h.change_ids else None, confidence=h.confidence,
                supporting=supporting, contradicting=h.contradicting,
                reasoning=_reasoning(h, topo)))
        if not hyps:
            return HypothesisSet(hypotheses=[], summary="No root-cause candidate yet - evidence is only symptomatic.")
        top = hyps[0]
        summary = (f"Leading hypothesis ({top.confidence:.0%}): {top.title}. "
                   + (f"Alternative: {hyps[1].title} ({hyps[1].confidence:.0%})." if len(hyps) > 1 else ""))
        return HypothesisSet(hypotheses=hyps, summary=summary)

    # --------------------------------------------------------------- critic
    async def critique(self, inp: CritiqueInput) -> Critique:
        await pace(self.pace_seconds * 0.8)
        topo = self.topo
        hyps = inp.hypotheses
        examined = {(f["analyst"], f["target"]) for f in inp.findings}
        done = {(t["analyst"], t["target"], t.get("window_minutes", 60) >= 150) for t in inp.task_log}
        tasks: list[InvestigationTask] = []
        gaps: list[str] = []

        def want(analyst: str, target: str, objective: str, window: int = 60, focus: str | None = None) -> None:
            wide = window >= 150
            if (analyst, target, wide) in done or any(t.analyst == analyst and t.target == target for t in tasks):
                return
            tasks.append(InvestigationTask(analyst=analyst, target=target, objective=objective,
                                           window_minutes=window, focus_time=focus))

        if not hyps:
            svc = inp.brief.get("service") or (inp.task_log[0]["target"] if inp.task_log else None)
            gaps.append("No candidate root cause yet.")
            if svc:
                want("changes", svc, f"Widen the change search around {svc} (4 h) - anything at all?", 240)
                for dep in [e.callee for e in topo.callees(svc)][:2]:
                    want("metrics", dep, f"Is {dep} anomalous?")
        else:
            top = hyps[0]
            comp = top["component"]
            ext = topo.get(comp).kind == "external" if comp in topo else False
            if ("metrics", comp) not in examined:
                gaps.append(f"{comp} has not been examined directly - is it the origin or a victim?")
                want("metrics", comp, f"Confirm whether {comp} is the origin: are its own golden signals anomalous, "
                                      f"and since when?", 60)
            if ("logs", comp) not in examined:
                gaps.append(f"No log evidence from {comp} itself yet.")
                want("logs", comp, f"What do {comp}'s own logs say since the onset? Look for a failure signature.")
            gradual_onset = _gradual_onset(inp.findings, comp)
            if top["category"] in ("memory_leak",) or gradual_onset:
                if not top.get("change_id"):
                    gaps.append(f"Gradual failure on {comp}: the trigger may predate the alert - widen the change "
                                f"window.")
                    want("changes", comp, f"Find the change that started the gradual degradation of {comp} "
                                          f"(leak / ramp) - widen the window to 4 h.", 240, gradual_onset)
            if ext and not any("status" in s for s in top.get("supporting", [])):
                gaps.append("Vendor status not confirmed.")
                want("changes", comp, f"Rule out internal changes and check {comp}'s vendor status page.", 120)
            if len(hyps) > 1:
                second = hyps[1]
                margin = top["confidence"] - second["confidence"]
                if margin < 0.15 and second["component"] != comp:
                    gaps.append(f"{second['title']} is still close ({margin:.0%} behind).")
                    if second["component"] in topo:
                        want("metrics", second["component"], f"Discriminate: is {second['component']} the origin "
                                                             f"or a victim of {comp}?")
                        want("logs", second["component"], f"Does {second['component']}'s log show who is driving it?")
            if top["confidence"] < 0.6 and not tasks:
                gaps.append(f"Confidence only {top['confidence']:.0%}.")
                want("dependencies", comp, f"Re-trace the hot path through {comp}.", 15)
        tasks = [t for t in tasks if t.target in topo][:4]
        if inp.round >= inp.max_rounds:
            verdict = "sufficient"
            reasoning = (f"Investigation budget reached ({inp.round}/{inp.max_rounds} rounds). Proceeding with the "
                         f"best-supported hypothesis" + (f"; open gaps: {'; '.join(gaps)}" if gaps else "") + ".")
            tasks = []
        elif tasks:
            verdict = "needs_more_evidence"
            reasoning = ("Not yet safe to act: " + " ".join(gaps) + f" Dispatching {len(tasks)} follow-up task(s).")
        else:
            verdict = "sufficient"
            top = hyps[0] if hyps else None
            n_sources = len({s.split(']')[0] for s in top['supporting']}) if top else 0
            reasoning = (f"{top['title']} is corroborated by {n_sources} independent sources at "
                         f"{top['confidence']:.0%} confidence, the root component was examined directly, and no "
                         f"alternative is close. Safe to plan remediation.") if top else "Nothing more to check."
        return Critique(verdict=verdict, reasoning=reasoning, gaps=gaps, follow_up_tasks=tasks)

    # -------------------------------------------------------------- planner
    async def plan(self, inp: PlanInput) -> PlanDraft:
        await pace(self.pace_seconds)
        h = inp.hypothesis
        facts = inp.facts
        tried = {(a["action"], _freeze(a.get("params", {}))) for a in inp.executed}
        failed_actions = {a["action"] for a in inp.executed}
        rejected = set(facts.get("rejected_actions", []))
        variables = {
            "component": h["component"],
            "previous_version": facts.get("previous_version"),
            "suggested_max_connections": facts.get("suggested_max_connections"),
            "flag": facts.get("flag"),
            "certificate": facts.get("certificate"),
            "caller": facts.get("top_caller"),
        }
        replan = bool(inp.executed) or bool(inp.feedback)
        actions: list[PlannedAction] = []
        used_runbooks: list[str] = []
        for rb in inp.runbooks:
            for spec in rb.get("actions", []):
                role = spec.get("role", "fix")
                params = _fill(spec.get("params", {}), variables)
                if params is None:
                    continue
                key = (spec["action"], _freeze(params))
                if key in tried or spec["action"] in rejected:
                    continue
                if not replan and role == "fallback":
                    continue
                if replan and role == "mitigation" and spec["action"] in failed_actions:
                    continue
                risk = spec.get("risk", ACTION_CATALOG[spec["action"]]["risk"])
                if RISK_ORDER[risk] < RISK_ORDER[ACTION_CATALOG[spec["action"]]["risk"]]:
                    risk = ACTION_CATALOG[spec["action"]]["risk"]
                actions.append(PlannedAction(
                    action=spec["action"], params=ActionParams(**params), risk=risk,
                    rationale=spec.get("rationale", ""), expected_effect=spec.get("expected_effect", "")))
                if rb["id"] not in used_runbooks:
                    used_runbooks.append(rb["id"])
                if role == "fix" or (replan and role == "fallback"):
                    break
            if any(a for a in actions if a.action != "restart_service"):
                break
        comp = h["component"]
        if actions:
            summary = (f"{'Re-plan: ' if replan else ''}" + "; then ".join(_describe(a) for a in actions)
                       + f" to address: {h['title']}.")
        else:
            summary = f"No pre-approved runbook action fits '{h['title']}'. Escalating to the {comp} owners."
        fallback = "Escalate to the owning team and keep the incident open."
        others = [spec for rb in inp.runbooks for spec in rb.get("actions", [])
                  if spec.get("role") == "fallback" and spec["action"] not in {a.action for a in actions}]
        if others:
            fallback = f"If this does not recover the service: {others[0]['action'].replace('_', ' ')} - " \
                       f"{others[0].get('rationale', '').rstrip('.')}. Otherwise escalate."
        impact = inp.brief.get("customer_impact", "Some customers are affected.")
        customer = (f"We're investigating an issue affecting our service. {impact} Our team has identified the cause "
                    f"and is applying a fix. Next update in 30 minutes.")
        return PlanDraft(summary=summary, actions=actions, runbook_ids=used_runbooks, fallback=fallback,
                         customer_message=customer)

    # --------------------------------------------------------------- report
    async def report(self, inp: ReportInput, on_token: TokenCallback | None = None) -> Report:
        await pace(self.pace_seconds)
        md = render_postmortem(inp)
        status = render_status_update(inp)
        if on_token:
            for chunk in re.split(r"(?<=\n)", md):
                await on_token(chunk)
        return Report(postmortem_markdown=md, status_update=status)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _search_phrase(sample: str) -> str:
    for rx in (r"x509[^:]*", r"too many clients", r"could not acquire connection", r"OutOfMemoryError|OOMKilled",
               r"PayRail authorize failed", r"application_name=[\w-]+@[\d.]+", r"slow call to [\w-]+",
               r"timeout after \d+ms waiting for [\w-]+", r"panic|Exception|KeyError", r"upstream [\w-]+ returned",
               r"traffic surge"):
        m = re.search(rx, sample)
        if m:
            return re.escape(m.group(0)) if "@" in m.group(0) else m.group(0)
    return sample.split(":")[0][:40]


def _self_hot(trace: dict | None) -> str | None:
    if not isinstance(trace, dict):
        return None
    for h in trace.get("hops", []):
        if h["hop"] == "self" and (h["delta_error_pct"] >= 0.5 or h["delta_ms"] >= 50):
            return f"self time {h['delta_ms']:+.0f} ms, errors {h['delta_error_pct']:+.1f} pts"
    return None


def _hot_path(topo: Topology, start: str, deps: list[ToolUse], traces: list[ToolUse]) -> list[dict]:
    """Follow the biggest latency / error contribution from `start`, hop by hop."""
    trace_by = {h.args.get("component"): h.artifact for h in traces if isinstance(h.artifact, dict)}
    info: dict[str, dict] = {}
    for h in deps:
        art = h.artifact if isinstance(h.artifact, dict) else {}
        for n in art.get("callees", []) + art.get("callers", []):
            info.setdefault(n["name"], n)
        if art.get("component"):
            info.setdefault(art["component"], {"name": art["component"], "health": art.get("health"),
                                               "anomalies": [a["text"] for a in art.get("anomalies", [])],
                                               "anomaly_detail": art.get("anomalies", []), "kind": art.get("kind")})
    path: list[dict] = []
    cur = start
    seen = {start}
    while cur in trace_by:
        tb = trace_by[cur]
        base_lat = topo.get(cur).latency_ms
        best = None
        for hop in tb.get("hops", []):
            if hop["hop"] == "self" or hop["hop"] not in topo:
                continue
            significant = hop["delta_ms"] >= max(40.0, 0.25 * base_lat) or hop["delta_error_pct"] >= 0.4
            if significant and (best is None or (hop["delta_ms"] / max(base_lat, 1) + hop["delta_error_pct"]) >
                                (best["delta_ms"] / max(base_lat, 1) + best["delta_error_pct"])):
                best = hop
        if best is None or best["hop"] in seen:
            break
        name = best["hop"]
        n = info.get(name, {})
        detail = n.get("anomaly_detail") or []
        real = [a for a in detail if not (a["metric"] in ("connections",) or a["metric"].startswith("throughput"))
                or a["direction"] == "up"]
        anomalous = bool(real) and n.get("health") != "healthy" or (
            bool(real) and any(a["metric"] in ("error_rate_pct", "memory_pct", "restarts") for a in real))
        path.append({"from": cur, "name": name, "kind": topo.get(name).kind, "delta_ms": best["delta_ms"],
                     "delta_err": best["delta_error_pct"], "health": n.get("health", "unknown"),
                     "anomalies": n.get("anomalies", []), "anomalous": anomalous,
                     "onset": min((a["onset"] for a in detail), default=None),
                     "gradual": any(a["pattern"] in GRADUAL for a in detail)})
        seen.add(name)
        cur = name
    return path


def _gradual_onset(findings: list[dict], component: str) -> str | None:
    for f in findings:
        for e in f.get("evidence", []):
            if e["component"] == component and any(p in e["summary"] for p in ("ramp", "sawtooth", "saw-tooth")):
                return f.get("anomaly_onset")
        if f["target"] == component and f["analyst"] == "metrics" and "gradual" in f.get("summary", ""):
            return f.get("anomaly_onset")
    return None


def _title(h: _Cand) -> str:
    comp = h.component
    detail = " ".join(h.details)
    version = re.search(r"-> (\d+\.\d+\.\d+)", detail) or re.search(r"@(\d+\.\d+\.\d+)", detail) or \
        re.search(r"build (\d+\.\d+\.\d+)", detail)
    v = f" {version.group(1)}" if version else ""
    chg = f" ({h.change_ids[0]})" if h.change_ids else ""
    if h.category == "bad_deploy":
        target = next(iter(h.links), None)
        if target or "query" in detail:
            return f"{comp}{v}{chg} introduced an N+1 query regression against {target or 'its database'}"
        if "exception" in detail or "self-time" in detail:
            return f"{comp}{v}{chg} ships an unhandled exception on its main code path"
        return f"Deploy of {comp}{v}{chg} caused a regression"
    if h.category == "memory_leak":
        return f"Memory leak in {comp}{v}{chg}: the heap fills until pods are OOMKilled"
    if h.category == "resource_exhaustion":
        flag = re.search(r"feature flag '([\w-]+)'", detail)
        return f"{comp} connection pool exhausted" + (
            f" by a traffic surge from flag '{flag.group(1)}'{chg}" if flag else "")
    if h.category == "external_dependency":
        return f"Third-party outage: {comp} is failing (503s / timeouts)"
    if h.category == "certificate_expiry":
        cert = re.search(r"certificate ([a-z0-9]+(?:-[a-z0-9]+)+) expired", detail)
        return f"Expired mTLS certificate{(' ' + cert.group(1)) if cert else ''} breaks {comp}'s database connections"
    if h.category == "config_change":
        return f"Config change on {comp}{chg} broke it"
    if h.category == "code_exception":
        return f"Unhandled exception inside {comp}"
    if h.category == "traffic_surge":
        return f"Traffic surge{chg} on {comp}"
    if h.category == "dependency_failure":
        return f"{comp} is degraded and failing its callers (origin not yet clear)"
    return f"{CATEGORY_LABEL.get(h.category, h.category)} on {comp}"


def _reasoning(h: _Cand, topo: Topology) -> str:
    sources = sorted(h.sources)
    upstream = topo.upstream(h.component)[:4]
    txt = (f"{len(sources)} independent source(s) ({', '.join(sources)}) point at {h.component}. "
           f"Failures propagate to {', '.join(upstream) or 'its callers'}, which matches the observed blast radius.")
    if h.contradicting:
        txt += " Weakened by: " + "; ".join(h.contradicting)
    return txt


def _freeze(d: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in (d or {}).items() if v is not None))


def _fill(params: dict, variables: dict) -> dict | None:
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            val = variables.get(v[1:-1])
            if val in (None, ""):
                if k == "to_version":
                    continue  # optional: the control plane defaults to the previous version
                return None
            out[k] = val
        else:
            out[k] = v
    return out


def _describe(a: PlannedAction) -> str:
    p = a.params.compact()
    target = p.get("service") or p.get("datastore") or p.get("flag") or p.get("certificate") or ""
    extra = ""
    if a.action == "rollback_deploy" and p.get("to_version"):
        extra = f" to {p['to_version']}"
    if a.action == "increase_connection_pool":
        extra = f" to {p.get('max_connections')}"
    if a.action == "toggle_feature_flag":
        extra = " on" if p.get("enabled") else " off"
    return f"{a.action.replace('_', ' ')} {target}{extra}".strip()


# ----------------------------------------------------------------------------
# reports
# ----------------------------------------------------------------------------

ACTION_ITEMS = {
    "bad_deploy": [("Add a query-count / error-budget gate to {c}'s CI (N+1 and exception guard)", "{team}", "P1"),
                   ("Canary {c} releases with automated latency + error analysis before 100%", "platform-sre", "P1"),
                   ("Fix the regression in {c} and add a regression test", "{team}", "P1")],
    "memory_leak": [("Bound the in-memory cache in {c} (max size + TTL)", "{team}", "P1"),
                    ("Alert on heap growth slope over 30 min, not only on OOMKills", "platform-sre", "P2"),
                    ("Add a 2-hour soak test to {c}'s release pipeline", "{team}", "P2")],
    "resource_exhaustion": [("Capacity-test campaigns before launch; pre-scale DB pools", "data-platform", "P1"),
                            ("Alert on connections / max_connections > 85%", "data-platform", "P1"),
                            ("Add a launch checklist for traffic-driving feature flags", "growth-marketing", "P2")],
    "external_dependency": [("Automate failover on sustained vendor error rate > 20% for 3 min", "payments", "P1"),
                            ("Enable the circuit breaker on payment-service -> payrail-api by default", "payments", "P2"),
                            ("Review PayRail SLA credits for this incident", "payments", "P3")],
    "certificate_expiry": [("Alert 30 / 14 / 7 days before any certificate expires", "security-platform", "P1"),
                           ("Automate mTLS client certificate renewal (cert-manager)", "security-platform", "P1"),
                           ("Add every client certificate to the inventory with an owner", "{team}", "P2")],
}


def _fmt_min(m: float | None) -> str:
    return "n/a" if m is None else f"{m:.0f} min"


def render_postmortem(inp: ReportInput) -> str:
    s = inp.state
    brief = s.get("brief", {})
    hyps = s.get("hypotheses") or []
    top = hyps[0] if hyps else None
    ver = s.get("verification") or {}
    status = ver.get("status", s.get("status", "unknown"))
    facts = inp.facts
    m = inp.metrics
    comp = top["component"] if top else s.get("alert", {}).get("service", "?")
    team = facts.get("team", "owning team")
    lines = [f"# Postmortem: {brief.get('title', 'Incident')}", ""]
    lines.append(f"**Severity** {brief.get('severity', '?')} · **Outcome** {status.replace('_', ' ')} · "
                 f"**Duration** {_fmt_min(m.get('duration'))} · **Reasoning engine** {facts.get('engine', '?')}")
    lines.append("")
    lines.append("## Summary")
    if top:
        lines.append(f"{brief.get('summary', '')}")
        lines.append("")
        lines.append(f"**Root cause ({top['confidence']:.0%} confidence):** {top['title']}.")
    lines.append("")
    lines.append("## Customer impact")
    lines.append(brief.get("customer_impact", "Unknown."))
    if facts.get("peak"):
        lines.append(f"Peak {facts['peak']}.")
    lines.append("")
    lines.append("## Key numbers")
    lines.append(f"- Time to detect (onset -> alert): **{_fmt_min(m.get('mttd'))}**")
    lines.append(f"- Time to root cause (alert -> diagnosis): **{_fmt_min(m.get('mtti'))}**")
    lines.append(f"- Time to mitigate (alert -> recovery): **{_fmt_min(m.get('mttr'))}**")
    lines.append(f"- Investigation: {m.get('rounds', 0)} round(s), {m.get('findings', 0)} findings, "
                 f"{m.get('tool_calls', 0)} tool calls")
    lines.append("")
    lines.append("## Timeline (UTC)")
    lines.append("| Time | Event |")
    lines.append("|---|---|")
    for ev in inp.timeline:
        lines.append(f"| {ev['time'][11:16]} | {ev['text'].replace('|', '/')} |")
    lines.append("")
    if top:
        lines.append("## Root cause")
        lines.append(top["reasoning"])
        lines.append("")
        lines.append("Evidence:")
        lines += [f"- {e}" for e in top["supporting"][:6]]
        if top.get("contradicting"):
            lines += [f"- *Against:* {e}" for e in top["contradicting"]]
        lines.append("")
    lines.append("## Investigation")
    for f in s.get("findings", []):
        lines.append(f"- Round {f['round']} · {f['analyst']} on `{f['target']}`: {f['headline']}")
    lines.append("")
    lines.append("## Resolution")
    for a in s.get("executed", []):
        ok = "applied" if a.get("ok") else f"FAILED ({a.get('message')})"
        lines.append(f"- {a.get('time', '')[11:16]} `{a['action']}` {a.get('params')} - {ok} "
                     f"(approved by {a.get('approved_by', 'policy')})")
    if not s.get("executed"):
        lines.append("- No automated action was executed.")
    if ver:
        lines.append(f"- Verification: **{ver.get('status', '?').replace('_', ' ')}** - {ver.get('summary', '')}")
    lines.append("")
    lines.append("## What went well")
    lines.append("- Parallel investigators covered metrics, logs, changes and dependencies within minutes.")
    if any(t.get("kind") == "approval" for t in inp.timeline):
        lines.append("- Risky actions were gated behind a human approval with full context.")
    lines.append("")
    lines.append("## What went wrong")
    if top and top["category"] in ("bad_deploy", "memory_leak"):
        lines.append(f"- The regression in {comp} reached 100% of production without being caught by tests or canary.")
    elif top and top["category"] == "resource_exhaustion":
        lines.append("- A traffic-driving launch was not capacity-tested against the database connection limit.")
    elif top and top["category"] == "certificate_expiry":
        lines.append("- No alert warned about the certificate expiring.")
    elif top and top["category"] == "external_dependency":
        lines.append("- Failover to the backup acquirer is manual and needed a human in the loop.")
    else:
        lines.append("- Root cause could not be fully confirmed automatically.")
    lines.append("")
    lines.append("## Action items")
    lines.append("| # | Action | Owner | Priority |")
    lines.append("|---|---|---|---|")
    items = ACTION_ITEMS.get(top["category"] if top else "", [("Complete root-cause analysis", "{team}", "P1")])
    for i, (text, owner, prio) in enumerate(items, 1):
        lines.append(f"| {i} | {text.format(c=comp, team=team)} | {owner.format(c=comp, team=team)} | {prio} |")
    lines.append("")
    lines.append("*Generated by Argus. Blameless format: this document is about systems and signals, not people.*")
    return "\n".join(lines)


def render_status_update(inp: ReportInput) -> str:
    s = inp.state
    ver = s.get("verification") or {}
    impact = s.get("brief", {}).get("customer_impact", "")
    st = ver.get("status")
    if st in ("resolved", "mitigated"):
        return (f"Resolved - {inp.facts.get('now_hhmm', '')} UTC. {impact.rstrip('.')} between "
                f"{inp.facts.get('onset_hhmm', '?')} and {inp.facts.get('recovered_hhmm', '?')} UTC. The issue has "
                f"been fixed and we are monitoring. We're sorry for the disruption.")
    return (f"Investigating - {impact} We have identified the likely cause and are working on a fix. "
            f"Next update in 30 minutes.")
