"""The simulated world: telemetry, change history, inventories and a control plane.

``World`` is deterministic (seeded) and *event-sourced*: its state is a pure
function of the scenario spec, the actions applied (and when), and the clock.
That makes every incident reproducible and lets a paused incident be rebuilt
after a server restart from the checkpointed graph state alone.

Time is measured in whole minutes since the start of the observation window
(``t = 0``).  ``clock`` is "now": telemetry exists for ``t < clock``.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .faults import Effect, EdgeMod, FaultModel, build_fault
from .topology import Topology

EMPTY_EFFECT = Effect()

# Every action the control plane accepts.  `risk` is the *minimum* risk level;
# the approval policy and the planner can only raise it, never lower it.
ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "restart_service": {
        "risk": "low", "params": {"service": "str"},
        "description": "Rolling restart of every pod of a service. Clears in-memory state; does not change code.",
    },
    "scale_service": {
        "risk": "low", "params": {"service": "str", "replicas": "int"},
        "description": "Change the replica count of a service (horizontal scaling).",
    },
    "rollback_deploy": {
        "risk": "high", "params": {"service": "str", "to_version": "str (optional, defaults to previous)"},
        "description": "Roll a service back to its previously deployed version.",
    },
    "increase_connection_pool": {
        "risk": "medium", "params": {"datastore": "str", "max_connections": "int"},
        "description": "Raise max_connections on a PostgreSQL datastore (and its PgBouncer pool).",
    },
    "toggle_feature_flag": {
        "risk": "medium", "params": {"flag": "str", "enabled": "bool"},
        "description": "Enable or disable a feature flag for all users.",
    },
    "rotate_certificate": {
        "risk": "medium", "params": {"certificate": "str"},
        "description": "Issue a fresh certificate from the internal CA and hot-reload it.",
    },
    "enable_circuit_breaker": {
        "risk": "medium", "params": {"service": "str", "dependency": "str"},
        "description": "Open a circuit breaker on calls from a service to a dependency (fail fast).",
    },
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

FIRST_NAMES = ("priya", "marco", "lena", "kenji", "amara", "noah", "sofia", "ravi", "ines", "tomas", "yara", "felix")


@dataclass
class Snap:
    latency: float
    error: float
    throughput: float
    cpu: float
    memory: float
    restarts: int
    connections: float | None
    lag: float | None
    lat_by_hop: dict[str, float]
    err_by_hop: dict[str, float]

    @property
    def lat_delta(self) -> float:
        return sum(self.lat_by_hop.values())

    @property
    def err_delta(self) -> float:
        return sum(self.err_by_hop.values())

    def metric(self, name: str) -> float:
        if name in ("latency_p99_ms", "query_latency_p95_ms"):
            return self.latency
        if name == "error_rate_pct":
            return self.error
        if name.startswith("throughput"):
            return self.throughput
        if name == "cpu_pct":
            return self.cpu
        if name == "memory_pct":
            return self.memory
        if name == "restarts":
            return float(self.restarts)
        if name == "connections":
            return float(self.connections or 0.0)
        if name == "consumer_lag":
            return float(self.lag or 0.0)
        raise KeyError(name)


@dataclass
class LogLine:
    t: int
    ts: str
    component: str
    level: str
    message: str


@dataclass
class ChangeEvent:
    id: str
    t: int
    kind: str
    component: str
    summary: str
    author: str
    details: dict = field(default_factory=dict)


@dataclass
class ScenarioSpec:
    id: str
    title: str
    fault: dict
    seed: int = 7
    onset: int = 104
    herrings: int = 3
    description: str = ""


class _NoiseBank:
    """Deterministic gaussian noise per (series, minute), safe to share across threads."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._series: dict[str, tuple[list[float], random.Random]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, t: int) -> float:
        with self._lock:
            entry = self._series.get(key)
            if entry is None:
                entry = ([], random.Random(f"{self.seed}:noise:{key}"))
                self._series[key] = entry
            values, rng = entry
            while len(values) <= t:
                values.append(max(-3.0, min(3.0, rng.gauss(0.0, 1.0))))
            return values[t]


class World:
    def __init__(self, spec: ScenarioSpec, anchor: datetime | None = None) -> None:
        self.spec = spec
        self.seed = spec.seed
        self.rng = random.Random(f"{spec.seed}:setup")
        self.topology = Topology()
        self.versions: dict[str, str] = {c.name: c.version for c in self.topology.components.values() if c.version}
        self.replicas: dict[str, int] = {c.name: c.replicas for c in self.topology.components.values() if c.replicas}
        self.max_connections: dict[str, int] = {c.name: c.max_connections for c in self.topology.components.values()
                                                if c.max_connections}
        self.flags: dict[str, dict] = {
            "new-footer-links": {"enabled": False, "owner": "storefront", "description": "Redesigned footer", "rollout_pct": 0},
            "recs-v2-ranking": {"enabled": True, "owner": "personalization", "description": "Two-tower ranking model", "rollout_pct": 100},
            "search-typo-tolerance": {"enabled": True, "owner": "catalog", "description": "Fuzzy matching in search", "rollout_pct": 100},
            "checkout-address-autocomplete": {"enabled": True, "owner": "checkout", "description": "Address autocomplete", "rollout_pct": 100},
        }
        self.certificates: dict[str, dict] = {}
        self.vendor_incidents: list[dict] = []
        self.changes: list[ChangeEvent] = []
        self.deploy_history: dict[str, list[dict]] = {}
        self.actions_log: list[dict] = []
        self.breakers: dict[tuple[str, str], int] = {}
        self._cap_history: dict[str, list[tuple[int, int]]] = {}
        self._noise = _NoiseBank(spec.seed)
        self._snap_cache: dict[int, dict[str, Snap]] = {}
        self._log_cache: dict[tuple[str, int], list[LogLine]] = {}
        self._fault_log_cache: dict[int, list[tuple[str, str, str]]] = {}
        self._lock = threading.RLock()
        self._change_seq = 4100 + self.rng.randint(0, 800)

        self._seed_inventory()
        self.fault: FaultModel = build_fault(spec.fault)
        self.fault.setup(self, spec.onset)
        self._add_red_herrings(spec.herrings)
        self.changes.sort(key=lambda c: c.t)

        # Evaluate the alert rule on the (noisy) telemetry to find when it fires.
        self.clock = spec.onset + 30  # temporary, lets the alert scan read ahead
        self.alert = self._evaluate_alert()
        self.clock = self.alert["fired_t"] + 2
        self._snap_cache.clear()
        now = (anchor or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
        self.anchor = now - timedelta(minutes=self.clock)
        self.start_clock = self.clock

    # ------------------------------------------------------------------ helpers
    def iso(self, t: float, second: int = 0) -> str:
        return (self.anchor + timedelta(minutes=t, seconds=second)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def hhmm(self, t: float) -> str:
        return (self.anchor + timedelta(minutes=t)).strftime("%H:%M")

    @staticmethod
    def poisson(rng: random.Random, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1

    @staticmethod
    def bump_version(version: str) -> str:
        major, minor, *_ = (version.split(".") + ["0", "0"])[:3]
        return f"{major}.{int(minor) + 1}.0"

    def pick_author(self, team: str) -> str:
        return f"{self.rng.choice(FIRST_NAMES)}.{team.split()[0]}@lumen.shop"

    def commit_sha(self) -> str:
        return "".join(self.rng.choice("0123456789abcdef") for _ in range(7))

    def add_change(self, t: int, kind: str, component: str, summary: str, author: str, details: dict | None = None) -> str:
        self._change_seq += self.rng.randint(1, 9)
        cid = f"CHG-{self._change_seq}"
        self.changes.append(ChangeEvent(cid, t, kind, component, summary, author, details or {}))
        if kind in ("deploy", "rollback"):
            self.deploy_history.setdefault(component, []).append(
                {"t": t, "from_version": (details or {}).get("from_version"),
                 "to_version": (details or {}).get("to_version"), "author": author, "change_id": cid, "kind": kind})
        return cid

    def set_certificate(self, name: str, owner: str, used_for: str, not_after_t: float, issuer: str) -> None:
        self.certificates[name] = {"name": name, "owner": owner, "used_for": used_for,
                                   "not_after_t": not_after_t, "issuer": issuer}

    # --------------------------------------------------------------- inventory
    def _seed_inventory(self) -> None:
        topo = self.topology
        # deploy history over the previous two weeks
        for comp in topo.components.values():
            if comp.kind != "service":
                continue
            hist = self.deploy_history.setdefault(comp.name, [])
            t = -self.rng.randint(2, 6) * 1440 - self.rng.randint(0, 600)
            hist.append({"t": t, "from_version": comp.previous_version, "to_version": comp.version,
                         "author": self.pick_author(comp.team), "change_id": None, "kind": "deploy"})
        # mTLS client certificates for every service -> SQL datastore edge
        for e in topo.edges:
            callee = topo.get(e.callee)
            if callee.sql:
                name = f"{e.callee}-client-{e.caller.removesuffix('-service')}"
                self.set_certificate(name, owner=e.caller, used_for=f"mTLS client auth {e.caller} -> {e.callee}",
                                     not_after_t=self.rng.randint(25, 160) * 1440, issuer="Lumen Internal CA G2")
        self.set_certificate("edge-tls-lumen-shop", owner="api-gateway", used_for="Public TLS for api.lumen.shop",
                             not_after_t=89 * 1440, issuer="Let's Encrypt R11")
        self.set_certificate("payrail-mtls-client", owner="payment-service", used_for="mTLS to PayRail",
                             not_after_t=211 * 1440, issuer="PayRail Partner CA")

    def _add_red_herrings(self, n: int) -> None:
        """Unrelated but plausible changes, so correlation is not trivial."""
        topo = self.topology
        root = self.fault.root
        busy = {root, self.fault.alert_service}
        candidates = [c.name for c in topo.components.values() if c.kind == "service" and c.name not in busy]
        self.rng.shuffle(candidates)
        onset = self.spec.onset
        templates = []
        if candidates:
            svc = candidates[0]
            frm = self.versions[svc]
            to = frm.rsplit(".", 1)[0] + "." + str(int(frm.rsplit(".", 1)[1]) + 1)
            templates.append(lambda: (self.rng.randint(onset - 45, onset - 12), "deploy", svc,
                                      f"Deployed {svc} {frm} -> {to} (rolling)",
                                      {"from_version": frm, "to_version": to, "commit": self.commit_sha()}, to))
        templates.append(lambda: (self.rng.randint(onset - 70, onset - 25), "config", "notification-service",
                                  "notification-service: LOG_LEVEL info -> debug (temporary, ticket COMMS-2214)",
                                  {"key": "LOG_LEVEL", "from": "info", "to": "debug"}, None))
        templates.append(lambda: (self.rng.randint(onset - 95, onset - 50), "feature_flag", "web-frontend",
                                  "Feature flag 'new-footer-links' enabled for 10% of users",
                                  {"flag": "new-footer-links", "from": False, "to": True, "rollout_pct": 10}, None))
        templates.append(lambda: (self.rng.randint(onset - 100, onset - 60), "certificate", "api-gateway",
                                  "Renewed public TLS certificate edge-tls-lumen-shop (auto-renewal)",
                                  {"certificate": "edge-tls-lumen-shop"}, None))
        if len(candidates) > 1:
            svc2 = candidates[1]
            reps = self.replicas.get(svc2, 3)
            templates.append(lambda: (self.rng.randint(onset - 80, onset - 30), "scaling", svc2,
                                      f"HPA for {svc2}: min replicas {reps} -> {reps + 1}",
                                      {"from": reps, "to": reps + 1}, None))
        self.rng.shuffle(templates)
        for make in templates[:n]:
            t, kind, comp, summary, details, new_version = make()
            if kind == "feature_flag":
                self.flags["new-footer-links"].update(enabled=True, rollout_pct=10)
            self.add_change(t, kind, comp, summary, self.pick_author(topo.get(comp).team), details)
            if new_version:
                self.versions[comp] = new_version

    # --------------------------------------------------------------- telemetry
    def snapshot(self, t: int) -> dict[str, Snap]:
        with self._lock:
            cached = self._snap_cache.get(t)
            if cached is not None:
                return cached
            snaps = self._compute(t)
            if t < self.clock:
                self._snap_cache[t] = snaps
            return snaps

    def _compute(self, t: int) -> dict[str, Snap]:
        effects, mods = self.fault.effects(t)
        topo = self.topology
        snaps: dict[str, Snap] = {}
        phase = (self.seed % 17) * 10
        diurnal = 1 + 0.05 * math.sin(2 * math.pi * (t + phase) / 180)
        for name in topo.leaves_first:
            comp = topo.get(name)
            eff = effects.get(name, EMPTY_EFFECT)
            lat_hops = dict(eff.lat_by_hop)
            err_hops = dict(eff.err_by_hop)
            for edge in topo.callees(name):
                child = snaps[edge.callee]
                mod = mods.get((name, edge.callee), EdgeMod())
                if self.breakers.get((name, edge.callee), 10**9) <= t:
                    mod = EdgeMod(lat_factor=min(mod.lat_factor, 0.02), err_factor=mod.err_factor)
                add_lat = min(child.lat_delta * min(1.0, edge.share * 6) * mod.lat_factor, float(edge.timeout_ms))
                add_err = child.err_delta * edge.share * (1.0 if edge.critical else 0.08) * mod.err_factor
                if add_lat > 0.5:
                    lat_hops[edge.callee] = lat_hops.get(edge.callee, 0.0) + add_lat
                if add_err > 0.005:
                    err_hops[edge.callee] = err_hops.get(edge.callee, 0.0) + add_err

            def n(metric: str) -> float:
                return self._noise.get(f"{name}:{metric}", t)

            traffic = eff.traffic
            base_lat = comp.latency_ms * (1 + 0.06 * n("lat")) * (1 + 0.12 * (traffic - 1))
            latency = max(0.5, base_lat + sum(lat_hops.values()))
            error = min(100.0, max(0.0, comp.error_pct * (1 + 0.3 * n("err")) + sum(err_hops.values())))
            throughput = max(0.0, comp.throughput * diurnal * traffic * eff.qps_mult * (1 + 0.03 * n("tp")))
            load = traffic * (eff.qps_mult ** 0.25)
            cpu = min(100.0, max(1.0, comp.cpu_pct * (0.65 + 0.35 * load) + eff.cpu + 1.6 * n("cpu")))
            memory = eff.memory if eff.memory is not None else comp.memory_pct
            memory = min(100.0, max(1.0, memory + 0.9 * n("mem")))
            connections = None
            if comp.max_connections:
                cap = self.cap_at(name, t)
                raw = eff.connections if eff.connections is not None else (comp.base_connections or 0) * traffic
                connections = min(float(cap), max(0.0, raw * (1 + 0.03 * n("conn"))))
            lag = 35 + 12 * n("lag") if comp.kind == "infra" else None
            snaps[name] = Snap(latency, error, throughput, cpu, memory, eff.restarts, connections, lag,
                               lat_hops, err_hops)
        return snaps

    def series(self, component: str, metric: str, start: int, end: int | None = None) -> list[tuple[int, float]]:
        end = self.clock if end is None else min(end, self.clock)
        start = max(0, start)
        self.topology.get(component)
        return [(t, self.snapshot(t)[component].metric(metric)) for t in range(start, end)]

    def _evaluate_alert(self) -> dict:
        svc = self.fault.alert_service
        metric = self.fault.alert_metric
        comp = self.topology.get(svc)
        threshold = comp.slo_latency_ms if metric == "latency_p99_ms" else comp.slo_error_pct
        need = 2 if self.fault.kind == "memory_leak" else 3
        run = 0
        fired = None
        for t in range(self.spec.onset - 10, self.spec.onset + 30):
            value = self._compute(t)[svc].metric(metric)
            run = run + 1 if value > threshold else 0
            if run >= need:
                fired = t + 1
                break
        if fired is None:  # pragma: no cover - scenarios are built to alert
            fired = self.spec.onset + 3
        value = self._compute(fired - 1)[svc].metric(metric)
        pretty = {"latency_p99_ms": "p99 latency", "error_rate_pct": "error rate"}[metric]
        unit = "ms" if metric == "latency_p99_ms" else "%"
        return {
            "id": f"ALR-{self.rng.randint(10000, 99999)}",
            "service": svc, "metric": metric, "threshold": threshold, "value": round(value, 2),
            "fired_t": fired, "for_minutes": need,
            "title": f"{svc} {pretty} {value:,.1f}{unit} above SLO {threshold:g}{unit}",
            "rule": f"{metric} > {threshold:g} for {need}m",
        }

    # -------------------------------------------------------------------- logs
    _ROUTES_FALLBACK = ("GET /",)

    def _fault_logs(self, t: int) -> list[tuple[str, str, str]]:
        with self._lock:
            cached = self._fault_log_cache.get(t)
            if cached is not None:
                return cached
            rng = random.Random(f"{self.seed}:faultlogs:{t}")
            out = self.fault.logs(t, rng)
            if t < self.clock:
                self._fault_log_cache[t] = out
            return out

    def _background(self, name: str, t: int, rng: random.Random, snap: Snap) -> list[tuple[str, str]]:
        comp = self.topology.get(name)
        lines: list[tuple[str, str]] = []
        if comp.kind == "service":
            routes = comp.routes or self._ROUTES_FALLBACK
            for _ in range(3):
                route = rng.choice(routes)
                failed = rng.random() < snap.error / 100.0
                status = rng.choice((500, 502, 503, 504)) if failed else rng.choice((200, 200, 200, 201, 204, 304))
                dur = int(snap.latency * rng.uniform(0.15, 0.95))
                level = "ERROR" if failed else "INFO"
                lines.append((level, f"{route} {status} {dur}ms"))
            if rng.random() < 0.25:
                lines.append(("INFO", f"health check passed (uptime {rng.randint(3600, 900000)}s)"))
            if rng.random() < 0.12:
                lines.append(("WARN", f"deprecated API version v1 called by client mobile-app/{rng.randint(410, 455)}"))
            deps = self.topology.callees(name)
            if deps and rng.random() < 0.08:
                dep = rng.choice(deps).callee
                lines.append(("WARN", f"retrying call to {dep} (attempt 2/3) after {rng.randint(40, 300)}ms"))
            if any(e.callee == "redis-cache" for e in deps) and rng.random() < 0.2:
                lines.append(("INFO", f"cache hit ratio {rng.randint(86, 97)}% (window 60s)"))
            if name == "web-frontend" and rng.random() < 0.1:
                lines.append(("WARN", "hydration text content mismatch on /product/:id"))
            if name == "notification-service" and self._flag_debug(t) and rng.random() < 0.9:
                lines.append(("DEBUG", f"rendered template order-confirmation for user u_{rng.randint(10**5, 10**6)}"))
        elif comp.sql:
            if rng.random() < 0.3:
                lines.append(("LOG", f"checkpoint complete: wrote {rng.randint(800, 5000)} buffers "
                                     f"({rng.uniform(0.5, 3.5):.1f}%); write={rng.uniform(5, 30):.3f} s"))
            if rng.random() < 0.2:
                table = rng.choice(("orders", "order_items", "products", "users", "sessions", "reservations"))
                lines.append(("LOG", f'automatic vacuum of table "{name}.public.{table}": index scans: 1'))
            if rng.random() < 0.08:
                client = rng.choice([e.caller for e in self.topology.callers(name)])
                lines.append(("LOG", f"duration: {rng.randint(110, 400)}.{rng.randint(100, 999)} ms  statement: "
                                     f"SELECT id, status FROM orders WHERE customer_id = $1 "
                                     f"[application_name={client}@{self.version_at(client, t) or '1.0.0'}]"))
            if rng.random() < 0.35:
                client = rng.choice([e.caller for e in self.topology.callers(name)])
                lines.append(("LOG", f"connection authorized: user={client.replace('-', '_')} database={name} "
                                     f"SSL enabled (protocol=TLSv1.3)"))
        elif name == "redis-cache":
            if rng.random() < 0.2:
                lines.append(("INFO", "DB saved on disk"))
            if rng.random() < 0.2:
                lines.append(("INFO", f"Background saving started by pid {rng.randint(1000, 9000)}"))
        elif name == "search-index":
            if rng.random() < 0.3:
                lines.append(("INFO", f"[gc][young][{rng.randint(1000, 9000)}] duration [{rng.randint(8, 40)}ms], collections [1]"))
            if rng.random() < 0.25:
                lines.append(("INFO", f"[products] refresh took [{rng.randint(40, 180)}ms]"))
        elif name == "event-bus":
            if rng.random() < 0.3:
                lines.append(("INFO", f"[Log partition=orders.events-{rng.randint(0, 11)}] Rolled new log segment "
                                      f"at offset {rng.randint(10**6, 10**7)}"))
        return lines

    def _flag_debug(self, t: int) -> bool:
        return any(c.kind == "config" and c.component == "notification-service" and c.t <= t for c in self.changes)

    def logs(self, component: str, start: int, end: int | None = None) -> list[LogLine]:
        end = self.clock if end is None else min(end, self.clock)
        self.topology.get(component)
        out: list[LogLine] = []
        for t in range(max(0, start), end):
            key = (component, t)
            with self._lock:
                cached = self._log_cache.get(key)
                if cached is None:
                    rng = random.Random(f"{self.seed}:logs:{component}:{t}")
                    snap = self.snapshot(t)[component]
                    raw = self._background(component, t, rng, snap)
                    raw += [(lvl, msg) for comp, lvl, msg in self._fault_logs(t) if comp == component]
                    seconds = [rng.randint(0, 59) for _ in raw]
                    cached = sorted((LogLine(t, self.iso(t, sec), component, lvl, msg)
                                     for (lvl, msg), sec in zip(raw, seconds)), key=lambda line: line.ts)
                    if t < self.clock:
                        self._log_cache[key] = cached
            out.extend(cached)
        return out

    # ------------------------------------------------------------------ traces
    def trace_breakdown(self, service: str, start: int, end: int | None = None) -> dict:
        """Where the time and the errors of a component's requests go, per call hop."""
        topo = self.topology
        comp = topo.get(service)
        end = self.clock if end is None else min(end, self.clock)
        window = range(max(0, start), end)
        base_window = range(5, 60)
        hops = ["self", *[e.callee for e in topo.callees(service)]]
        hop_set = set(hops)

        def avg(values: list[float]) -> float:
            return sum(values) / len(values) if values else 0.0

        rows = []
        for hop in hops:
            if hop == "self":
                base_ms = comp.latency_ms * 0.5
                share = 1.0
                base_err = comp.error_pct
            else:
                e = topo.edge(service, hop)
                callee = topo.get(hop)
                share = e.share
                base_ms = callee.latency_ms * min(1.0, e.share * 6) * 0.5
                base_err = (callee.error_pct or 0.0) * e.share
            lat_now = avg([self.snapshot(t)[service].lat_by_hop.get(hop, 0.0) for t in window])
            err_now = avg([self.snapshot(t)[service].err_by_hop.get(hop, 0.0) for t in window])
            lat_base = avg([self.snapshot(t)[service].lat_by_hop.get(hop, 0.0) for t in base_window])
            err_base = avg([self.snapshot(t)[service].err_by_hop.get(hop, 0.0) for t in base_window])
            rows.append({
                "hop": hop, "calls_per_request": share,
                "baseline_ms": round(base_ms + lat_base, 1), "current_ms": round(base_ms + lat_now, 1),
                "delta_ms": round(lat_now - lat_base, 1),
                "baseline_error_pct": round(base_err + err_base, 2), "current_error_pct": round(base_err + err_now, 2),
                "delta_error_pct": round(err_now - err_base, 2),
                "kind": "self" if hop == "self" else topo.get(hop).kind,
            })
        # extra hops that only exist during the incident (e.g. a failover acquirer)
        extra = set()
        for t in window:
            extra |= set(self.snapshot(t)[service].lat_by_hop) - hop_set
        for hop in sorted(extra):
            lat_now = avg([self.snapshot(t)[service].lat_by_hop.get(hop, 0.0) for t in window])
            rows.append({"hop": hop, "calls_per_request": 1.0, "baseline_ms": 0.0, "current_ms": round(lat_now, 1),
                         "delta_ms": round(lat_now, 1), "baseline_error_pct": 0.0, "current_error_pct": 0.0,
                         "delta_error_pct": 0.0, "kind": "external"})
        rows.sort(key=lambda r: (abs(r["delta_ms"]) / max(comp.latency_ms, 1) + abs(r["delta_error_pct"])), reverse=True)
        return {"service": service, "window": [self.iso(window.start), self.iso(window.stop)], "hops": rows}

    def cap_at(self, name: str, t: int) -> int:
        cap = self.topology.get(name).max_connections or 0
        for at, value in self._cap_history.get(name, []):
            if t >= at:
                cap = value
        return cap

    def version_at(self, service: str, t: int) -> str | None:
        version = self.topology.get(service).version
        for h in sorted(self.deploy_history.get(service, []), key=lambda h: h["t"]):
            if h["t"] <= t and h.get("to_version"):
                version = h["to_version"]
        return version

    # ------------------------------------------------------------ inventories
    def changes_between(self, start: float, end: float | None = None) -> list[ChangeEvent]:
        end = self.clock if end is None else end
        return [c for c in self.changes if start <= c.t < end]

    def vendor_status(self) -> list[dict]:
        active = [v for v in self.vendor_incidents if v["t"] <= self.clock]
        status = {
            "PayRail": {"vendor": "PayRail", "component": "payrail-api", "status": "operational",
                        "title": "All systems operational", "since": None},
            "Postmark": {"vendor": "Postmark", "component": "notification-service (email)", "status": "operational",
                         "title": "All systems operational", "since": None},
        }
        for v in active:
            status[v["vendor"]] = {"vendor": v["vendor"], "component": v["component"], "status": v["status"],
                                   "title": v["title"], "message": v["message"], "since": self.iso(v["t"])}
        return list(status.values())

    def certificate_list(self) -> list[dict]:
        out = []
        for c in self.certificates.values():
            minutes_left = c["not_after_t"] - self.clock
            out.append({**{k: v for k, v in c.items() if k != "not_after_t"},
                        "not_after": self.iso(c["not_after_t"]),
                        "expired": minutes_left <= 0,
                        "expires_in_hours": round(minutes_left / 60, 1)})
        return sorted(out, key=lambda c: c["expires_in_hours"])

    # ----------------------------------------------------------- control plane
    def advance(self, minutes: int) -> None:
        with self._lock:
            self.clock += max(0, int(minutes))

    def set_clock(self, t: int) -> None:
        """Move "now" forward to `t` (idempotent; time never goes backwards)."""
        with self._lock:
            self.clock = max(self.clock, int(t))

    def apply_action(self, action: str, params: dict, action_id: str | None = None) -> dict:
        """Validate and execute a remediation action against the simulated estate.

        `action_id` makes the call idempotent: re-applying the same id returns the first result.
        """
        with self._lock:
            if action_id:
                prior = next((a for a in self.actions_log if a.get("action_id") == action_id), None)
                if prior is not None:
                    return {k: v for k, v in prior.items() if k not in ("t", "action", "action_id")}
            result = self._apply(action, dict(params or {}))
            self.actions_log.append({"t": self.clock, "action": action, "action_id": action_id, **result})
            return result

    def _apply(self, action: str, p: dict) -> dict:
        topo = self.topology
        t = self.clock
        if action not in ACTION_CATALOG:
            return {"ok": False, "message": f"unknown action '{action}'"}

        def need_service(key: str = "service") -> str:
            name = p.get(key)
            if name not in topo or topo.get(name).kind != "service":
                raise ValueError(f"'{name}' is not a service")
            return name

        try:
            if action == "restart_service":
                svc = need_service()
                cid = self.add_change(t, "restart", svc, f"Rolling restart of {svc}", "argus-agent")
            elif action == "scale_service":
                svc = need_service()
                replicas = int(p.get("replicas", 0))
                if not 1 <= replicas <= 60:
                    raise ValueError("replicas must be between 1 and 60")
                before = self.replicas[svc]
                self.replicas[svc] = replicas
                cid = self.add_change(t, "scaling", svc, f"Scaled {svc} {before} -> {replicas} replicas",
                                      "argus-agent", {"from": before, "to": replicas})
            elif action == "rollback_deploy":
                svc = need_service()
                hist = sorted(self.deploy_history.get(svc, []), key=lambda h: h["t"])
                current = self.versions[svc]
                previous = next((h["from_version"] for h in reversed(hist) if h["to_version"] == current), None)
                target = p.get("to_version") or previous
                if not target:
                    raise ValueError(f"no previous version recorded for {svc}")
                if target == current:
                    raise ValueError(f"{svc} is already running {current}")
                p["to_version"] = target
                cid = self.add_change(t, "rollback", svc, f"Rolled back {svc} {current} -> {target}", "argus-agent",
                                      {"from_version": current, "to_version": target})
                self.versions[svc] = target
            elif action == "increase_connection_pool":
                db = p.get("datastore")
                if db not in topo or not topo.get(db).sql:
                    raise ValueError(f"'{db}' is not a PostgreSQL datastore")
                new = int(p.get("max_connections", 0))
                current = self.max_connections[db]
                if new <= current:
                    raise ValueError(f"max_connections must be above the current {current}")
                if new > 2000:
                    raise ValueError("max_connections above 2000 needs a DBA change request")
                self.max_connections[db] = new
                self._cap_history.setdefault(db, []).append((t, new))
                cid = self.add_change(t, "config", db, f"{db}: max_connections {current} -> {new}", "argus-agent",
                                      {"key": "max_connections", "from": current, "to": new})
            elif action == "toggle_feature_flag":
                flag = p.get("flag")
                if flag not in self.flags:
                    raise ValueError(f"unknown feature flag '{flag}'")
                enabled = p.get("enabled")
                if isinstance(enabled, str):
                    enabled = enabled.lower() in ("1", "true", "yes", "on")
                enabled = bool(enabled)
                p["enabled"] = enabled
                before = self.flags[flag]["enabled"]
                self.flags[flag].update(enabled=enabled, rollout_pct=100 if enabled else 0)
                cid = self.add_change(t, "feature_flag", "web-frontend",
                                      f"Feature flag '{flag}' {'enabled' if enabled else 'disabled'}", "argus-agent",
                                      {"flag": flag, "from": before, "to": enabled})
            elif action == "rotate_certificate":
                name = p.get("certificate")
                if name not in self.certificates:
                    raise ValueError(f"unknown certificate '{name}'")
                self.certificates[name]["not_after_t"] = t + 90 * 1440
                cid = self.add_change(t, "certificate", self.certificates[name]["owner"],
                                      f"Rotated certificate {name} (valid 90 days)", "argus-agent",
                                      {"certificate": name})
            elif action == "enable_circuit_breaker":
                svc = need_service()
                dep = p.get("dependency")
                if not topo.edge(svc, dep):
                    raise ValueError(f"{svc} does not call '{dep}'")
                self.breakers.setdefault((svc, dep), t)
                cid = self.add_change(t, "config", svc, f"Circuit breaker enabled on {svc} -> {dep}", "argus-agent",
                                      {"dependency": dep})
            else:  # pragma: no cover
                raise ValueError(action)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": str(exc)}
        note = self.fault.on_action(t, action, p)
        self.changes.sort(key=lambda c: c.t)
        return {"ok": True, "message": f"{action} applied at {self.hhmm(t)}", "change_id": cid,
                "params": p, "sim_note": note}

    # ------------------------------------------------------------ public view
    def health(self, component: str) -> str:
        return self.health_at(component, self.clock)

    def health_at(self, component: str, clock: int) -> str:
        """Health judged on the three minutes before `clock` (lets a recording replay past health)."""
        comp = self.topology.get(component)
        clock = min(clock, self.clock)
        recent = [self.snapshot(t)[component] for t in range(max(0, clock - 3), clock)]
        if not recent:
            return "unknown"
        lat = sum(s.latency for s in recent) / len(recent)
        err = sum(s.error for s in recent) / len(recent)
        mem = max(s.memory for s in recent)
        restarts = sum(s.restarts for s in recent)
        conn_util = None
        if comp.max_connections:
            cap = self.cap_at(component, max(0, clock - 1))
            conn_util = max((s.connections or 0) for s in recent) / cap
        if (comp.slo_error_pct and err > comp.slo_error_pct * 3) or err > 5 or restarts or \
                (conn_util is not None and conn_util > 0.97):
            return "critical"
        if (comp.slo_latency_ms and lat > comp.slo_latency_ms) or lat > comp.latency_ms * 2.5 or \
                (comp.slo_error_pct and err > comp.slo_error_pct) or err > 2 or mem > 88 or \
                (conn_util is not None and conn_util > 0.85):
            return "degraded"
        return "healthy"

    def public_state(self) -> dict:
        return {
            "clock": self.clock, "now": self.iso(self.clock), "start_clock": self.start_clock,
            "window_start": self.iso(0),
            "health": {name: self.health(name) for name in self.topology.components},
            "versions": dict(self.versions),
        }


def snap_to_dict(s: Snap) -> dict:  # pragma: no cover - debugging helper
    return asdict(s)
