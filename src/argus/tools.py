"""LangChain tools over a telemetry backend.

Each tool returns ``(content, artifact)``: compact text written for an LLM, plus a
structured artifact that the deterministic reasoner and the web UI consume.  The
same tools are used by the Claude-powered investigators (via tool calling) and by
the offline reasoner, so both see exactly the same evidence.

Read-only tools only.  Mutating actions (rollbacks, restarts, ...) are *not*
exposed to the model: they run in the ``execute`` node, after the approval gate.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from .analysis import detect_anomaly, fmt_num, mine_templates
from .knowledge import RunbookRetriever
from .sim.world import World

ANALYST_TOOLS: dict[str, tuple[str, ...]] = {
    "metrics": ("get_golden_signals", "query_metric"),
    "logs": ("get_log_patterns", "search_logs"),
    "changes": ("list_recent_changes", "get_deploy_history", "list_certificates", "check_vendor_status",
                "list_feature_flags"),
    "dependencies": ("get_service_dependencies", "get_trace_breakdown", "check_vendor_status"),
}


def _unknown(world: World, name: str) -> tuple[str, dict]:
    known = ", ".join(sorted(world.topology.components))
    return f"Unknown component '{name}'. Known components: {known}.", {"error": f"unknown component {name}"}


def signals(world: World, component: str, window_minutes: int = 60) -> dict:
    """Golden signals + anomaly detection for one component (shared by several tools and the UI)."""
    comp = world.topology.get(component)
    full = {m: world.series(component, m, 0) for m in comp.metrics}
    start = max(0, world.clock - window_minutes)
    metrics: dict[str, Any] = {}
    anomalies = []
    for m, pts in full.items():
        a = detect_anomaly(pts, m)
        recent = [v for t, v in pts if t >= world.clock - 3]
        base = [v for t, v in pts if 5 <= t < 60]
        entry = {
            "current": round(sum(recent) / len(recent), 2) if recent else None,
            "baseline": round(sorted(base)[len(base) // 2], 2) if base else None,
            "tail": [round(v, 2) for t, v in pts if t >= start][-30:],
            "anomaly": None,
        }
        if a:
            entry["anomaly"] = {"onset_t": a.onset_t, "onset": world.iso(a.onset_t), "direction": a.direction,
                                "pattern": a.pattern, "baseline": round(a.baseline, 2),
                                "value": round(a.headline_value, 2), "peak": round(a.peak, 2),
                                "change_pct": round(a.change_pct, 1), "text": a.describe(world.hhmm)}
            anomalies.append({"metric": m, **entry["anomaly"]})
        metrics[m] = entry
    if comp.max_connections:
        metrics["connections"]["max"] = world.max_connections.get(component, comp.max_connections)
    anomalies.sort(key=lambda a: a["onset_t"])
    return {"component": component, "kind": comp.kind, "team": comp.team, "health": world.health(component),
            "window": [world.iso(start), world.iso(world.clock)],
            "slo": {"latency_p99_ms": comp.slo_latency_ms, "error_rate_pct": comp.slo_error_pct} if comp.has_slo else None,
            "metrics": metrics, "anomalies": anomalies}


def _signals_text(world: World, s: dict) -> str:
    comp = world.topology.get(s["component"])
    lines = [f"{s['component']} ({comp.kind}, {comp.tech}, team {comp.team}) - health: {s['health'].upper()}",
             f"window {s['window'][0]} .. {s['window'][1]}; baseline {world.iso(5)} .. {world.iso(59)}"]
    if s["slo"]:
        slo = s["slo"]
        lines.append(f"SLO: p99 <= {slo['latency_p99_ms']} ms, errors <= {slo['error_rate_pct']}%")
    if s["anomalies"]:
        lines.append("ANOMALIES (earliest first):")
        lines += [f"  - {a['text']}" for a in s["anomalies"]]
    else:
        lines.append("ANOMALIES: none - all metrics within normal range")
    normal = [f"{m} {fmt_num(v['current'])} (baseline {fmt_num(v['baseline'])})"
              for m, v in s["metrics"].items() if not v["anomaly"] and v["current"] is not None]
    if normal:
        lines.append("Normal: " + "; ".join(normal))
    if "max" in s["metrics"].get("connections", {}):
        c = s["metrics"]["connections"]
        lines.append(f"connections: {fmt_num(c['current'])} / max_connections {c['max']}")
    return "\n".join(lines)


def build_tools(world: World) -> dict[str, BaseTool]:
    """Instantiate the read-only investigation tools bound to one world."""
    topo = world.topology
    retriever = RunbookRetriever(k=3)

    @tool(response_format="content_and_artifact")
    def get_golden_signals(component: str, window_minutes: int = 60) -> tuple[str, dict]:
        """Golden signals (latency, errors, traffic, saturation) of one component compared with its baseline,
        with anomaly detection: onset time, pattern (step / ramp / spike / sawtooth / drop) and change %."""
        if component not in topo:
            return _unknown(world, component)
        s = signals(world, component, window_minutes)
        return _signals_text(world, s), s

    @tool(response_format="content_and_artifact")
    def query_metric(component: str, metric: str, window_minutes: int = 45) -> tuple[str, dict]:
        """Raw 1-minute series of a single metric (e.g. memory_pct, connections, restarts, throughput_qps)."""
        if component not in topo:
            return _unknown(world, component)
        comp = topo.get(component)
        if metric not in comp.metrics:
            return (f"{component} has no metric '{metric}'. Available: {', '.join(comp.metrics)}",
                    {"error": "unknown metric"})
        pts = world.series(component, metric, world.clock - window_minutes)
        step = max(1, len(pts) // 30)
        shown = ", ".join(f"{world.hhmm(t)} {fmt_num(v)}" for t, v in pts[::step])
        return (f"{component}.{metric} (every {step} min): {shown}",
                {"component": component, "metric": metric, "points": [[world.iso(t), round(v, 3)] for t, v in pts]})

    def _log_source(component: str, start: int) -> tuple[list, str | None]:
        comp = topo.get(component)
        if comp.kind != "external":
            return world.logs(component, start), None
        token = component.split("-")[0]
        lines = []
        for e in topo.callers(component):
            lines += [ln for ln in world.logs(e.caller, start) if token.lower() in ln.message.lower()]
        note = (f"{component} is a third-party API (no server logs). Showing client-side log lines that mention it "
                f"from: {', '.join(e.caller for e in topo.callers(component))}.")
        return lines, note

    @tool(response_format="content_and_artifact")
    def get_log_patterns(component: str, window_minutes: int = 60, since: str | None = None) -> tuple[str, dict]:
        """Cluster a component's logs into templates and compare the incident period with the period before it.
        Flags NEW and SURGING patterns and matches them against known failure signatures. `since` (ISO time)
        sets the split point; it defaults to a few minutes before the alert fired."""
        if component not in topo:
            return _unknown(world, component)
        start = max(0, world.clock - window_minutes)
        split = world.alert["fired_t"] - 8
        if since:
            split = _t_from_iso(world, since, split)
        split = max(start + 5, min(split, world.clock - 2))
        lines, note = _log_source(component, start)
        templates = mine_templates(lines, split, topo.components)
        rows = [{"template": t.template, "level": t.level, "status": t.status, "count": t.count, "before": t.before,
                 "after": t.after, "first_seen": world.iso(t.first_seen_t), "first_seen_t": t.first_seen_t,
                 "last_seen": world.iso(t.last_seen_t), "sample": t.sample, "signature": t.signature,
                 "signature_label": t.signature_label} for t in templates]
        text = [f"{component}: {len(lines)} log lines in {window_minutes} min, {len(rows)} patterns. "
                f"Split at {world.hhmm(split)} (before vs after)."]
        if note:
            text.insert(0, note)
        for r in rows[:9]:
            sig = f"  <- {r['signature_label']}" if r["signature"] else ""
            text.append(f"[{r['status'].upper():8s}] {r['level']:5s} before={r['before']:<3d} after={r['after']:<3d} "
                        f"first {world.hhmm(r['first_seen_t'])}: {r['template'][:150]}{sig}")
        return "\n".join(text), {"component": component, "split": world.iso(split), "split_t": split,
                                 "total_lines": len(lines), "templates": rows[:25], "note": note}

    @tool(response_format="content_and_artifact")
    def search_logs(component: str, query: str = "", level: str = "WARN", window_minutes: int = 30,
                    limit: int = 12) -> tuple[str, dict]:
        """Search a component's recent logs. `query` is a case-insensitive substring or regex; `level` is the
        minimum level (DEBUG, INFO, WARN, ERROR). Returns the newest matching lines."""
        if component not in topo:
            return _unknown(world, component)
        rank = {"DEBUG": 0, "INFO": 1, "LOG": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}
        min_rank = rank.get(level.upper(), 2)
        lines, note = _log_source(component, max(0, world.clock - window_minutes))
        try:
            rx = re.compile(query, re.I) if query else None
        except re.error:
            rx = re.compile(re.escape(query), re.I)
        hits = [ln for ln in lines if rank.get(ln.level, 1) >= min_rank and (rx is None or rx.search(ln.message))]
        shown = hits[-limit:]
        body = "\n".join(f"{ln.ts} {ln.level:5s} {ln.message[:220]}" for ln in shown) or "(no matching lines)"
        head = f"{len(hits)} matching lines in {component} (last {window_minutes} min); newest {len(shown)}:"
        return "\n".join(filter(None, [note, head, body])), {
            "component": component, "query": query, "matches": len(hits),
            "lines": [{"ts": ln.ts, "t": ln.t, "level": ln.level, "message": ln.message} for ln in shown]}

    @tool(response_format="content_and_artifact")
    def list_recent_changes(window_minutes: int = 120, component: str | None = None) -> tuple[str, dict]:
        """Change log (deploys, rollbacks, feature flags, config, scaling, certificates) for the whole estate,
        newest first. Optionally filter by component."""
        changes = world.changes_between(world.clock - window_minutes)
        if component:
            changes = [c for c in changes if c.component == component]
        alert_t = world.alert["fired_t"]
        rows = []
        for c in sorted(changes, key=lambda c: c.t, reverse=True):
            rows.append({"id": c.id, "t": c.t, "time": world.iso(c.t), "kind": c.kind, "component": c.component,
                         "summary": c.summary, "author": c.author, "details": c.details,
                         "minutes_before_alert": alert_t - c.t})
        text = [f"{len(rows)} changes in the last {window_minutes} min (alert fired {world.hhmm(alert_t)}):"]
        for r in rows:
            rel = r["minutes_before_alert"]
            when = f"{rel} min before alert" if rel >= 0 else f"{-rel} min after alert"
            text.append(f"- {r['id']} {world.hhmm(r['t'])} ({when}) [{r['kind']}] {r['component']}: {r['summary']} "
                        f"- by {r['author']}")
        if not rows:
            text.append("(none)")
        return "\n".join(text), {"window_minutes": window_minutes, "changes": rows, "now_t": world.clock,
                                 "now": world.iso(world.clock), "alert_t": alert_t}

    @tool(response_format="content_and_artifact")
    def get_deploy_history(service: str) -> tuple[str, dict]:
        """Deployed versions of a service over the last two weeks, newest first, plus the running version."""
        if service not in topo:
            return _unknown(world, service)
        hist = sorted(world.deploy_history.get(service, []), key=lambda h: h["t"], reverse=True)
        rows = [{**h, "time": world.iso(h["t"])} for h in hist]
        text = [f"{service} is running {world.versions.get(service)}."]
        for r in rows:
            ago = world.clock - r["t"]
            age = f"{ago} min ago" if ago < 600 else f"{ago / 1440:.1f} days ago"
            text.append(f"- {r['time']} ({age}) {r['kind']} {r['from_version']} -> {r['to_version']} by {r['author']}")
        return "\n".join(text), {"service": service, "running": world.versions.get(service), "history": rows}

    @tool(response_format="content_and_artifact")
    def list_certificates(component: str | None = None) -> tuple[str, dict]:
        """TLS / mTLS certificate inventory with expiry. Optionally only certificates owned by a component."""
        certs = world.certificate_list()
        if component:
            certs = [c for c in certs if c["owner"] == component or component in c["used_for"]]
        text = [f"{len(certs)} certificates" + (f" related to {component}" if component else "") + ":"]
        for c in certs:
            state = "EXPIRED" if c["expired"] else f"expires in {c['expires_in_hours']:.0f} h"
            text.append(f"- {c['name']} (owner {c['owner']}; {c['used_for']}) not_after {c['not_after']} -> {state}")
        return "\n".join(text), {"certificates": certs}

    @tool(response_format="content_and_artifact")
    def check_vendor_status(vendor: str | None = None) -> tuple[str, dict]:
        """Public status pages of third-party providers (e.g. PayRail payments)."""
        rows = world.vendor_status()
        if vendor:
            rows = [r for r in rows if vendor.lower() in (r["vendor"] + r["component"]).lower()] or rows
        text = [f"- {r['vendor']} ({r['component']}): {r['status'].upper()} - {r['title']}"
                + (f" (since {r['since']}) {r.get('message', '')}" if r.get("since") else "") for r in rows]
        return "\n".join(text), {"vendors": rows}

    @tool(response_format="content_and_artifact")
    def list_feature_flags() -> tuple[str, dict]:
        """All feature flags with their state, owner and description."""
        rows = [{"flag": k, **v} for k, v in sorted(world.flags.items())]
        text = [f"- {r['flag']}: {'ON' if r['enabled'] else 'off'} ({r['rollout_pct']}%) owner {r['owner']} - "
                f"{r['description']}" for r in rows]
        return "\n".join(text), {"flags": rows}

    @tool(response_format="content_and_artifact")
    def get_service_dependencies(component: str) -> tuple[str, dict]:
        """A component's callers and dependencies with each neighbour's current health and anomalies."""
        if component not in topo:
            return _unknown(world, component)

        def neighbour(name: str, edge) -> dict:
            s = signals(world, name, 30)
            return {"name": name, "kind": topo.get(name).kind, "share": edge.share, "critical": edge.critical,
                    "health": s["health"], "anomalies": [a["text"] for a in s["anomalies"]],
                    "anomaly_detail": s["anomalies"]}

        own = signals(world, component, 30)
        callees = [neighbour(e.callee, e) for e in topo.callees(component)]
        callers = [neighbour(e.caller, e) for e in topo.callers(component)]
        text = [f"{component}: health {own['health'].upper()}"
                + (f"; anomalies: {'; '.join(a['text'] for a in own['anomalies'])}" if own["anomalies"] else "")]
        text.append("Depends on:" if callees else "Depends on: nothing (leaf component)")
        for n in callees:
            text.append(f"  -> {n['name']} ({n['kind']}, {int(n['share'] * 100)}% of requests"
                        f"{', critical' if n['critical'] else ', optional'}): {n['health'].upper()}"
                        + (f" | {'; '.join(n['anomalies'][:3])}" if n["anomalies"] else ""))
        text.append("Called by: " + (", ".join(f"{n['name']} ({n['health']})" for n in callers) or "edge traffic"))
        return "\n".join(text), {"component": component, "health": own["health"], "kind": topo.get(component).kind,
                                 "anomalies": own["anomalies"], "callees": callees, "callers": callers}

    @tool(response_format="content_and_artifact")
    def get_trace_breakdown(component: str, window_minutes: int = 10) -> tuple[str, dict]:
        """Distributed-tracing breakdown of a component's requests: latency and error contribution per call hop
        (self time vs each dependency), current window vs baseline. Use it to follow the hot path."""
        if component not in topo:
            return _unknown(world, component)
        if topo.get(component).kind in ("datastore", "external", "infra"):
            return (f"{component} is a {topo.get(component).kind}; it has no outgoing call spans. "
                    f"Use get_golden_signals / logs instead.", {"component": component, "hops": [], "leaf": True})
        tb = world.trace_breakdown(component, world.clock - window_minutes)
        text = [f"Trace breakdown for {component} (last {window_minutes} min vs baseline), biggest change first:"]
        for h in tb["hops"]:
            text.append(f"- {h['hop']:24s} {h['baseline_ms']:>7.0f} -> {h['current_ms']:>7.0f} ms "
                        f"({h['delta_ms']:+.0f}); errors {h['baseline_error_pct']:.2f}% -> "
                        f"{h['current_error_pct']:.2f}% ({h['delta_error_pct']:+.2f} pts)")
        return "\n".join(text), tb

    @tool(response_format="content_and_artifact")
    def search_runbooks(query: str) -> tuple[str, dict]:
        """Search the team's runbooks (mitigation procedures with pre-approved actions)."""
        docs = retriever.invoke(query)
        text = []
        for d in docs:
            text.append(f"## {d.metadata['id']}: {d.metadata['title']}\n{d.page_content[:1200]}")
        return "\n\n".join(text) or "No runbook matched.", {
            "runbooks": [{"id": d.metadata["id"], "title": d.metadata["title"],
                          "actions": d.metadata.get("actions", [])} for d in docs]}

    tools = [get_golden_signals, query_metric, get_log_patterns, search_logs, list_recent_changes,
             get_deploy_history, list_certificates, check_vendor_status, list_feature_flags,
             get_service_dependencies, get_trace_breakdown, search_runbooks]
    return {t.name: t for t in tools}


def _t_from_iso(world: World, iso: str, default: int) -> int:
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int((dt - world.anchor).total_seconds() // 60)
    except (ValueError, TypeError):
        return default
