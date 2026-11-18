"""Fault models: how a root cause shows up in telemetry and how it reacts to fixes.

Each fault describes, for any minute ``t`` of simulated time,

* the *root effects* it has on components (extra latency / errors attributed to a
  call hop, traffic multipliers, memory, connections, restarts) -- the world then
  propagates those effects up the dependency graph to callers;
* the log lines it produces; and
* how it responds to remediation actions (a rollback fixes a bad deploy, a
  restart only resets a memory leak for a while, scaling up the callers makes a
  connection-pool exhaustion *worse*, ...).

The agent never sees these classes.  It only sees metrics, logs, traces, change
events and inventories through tools -- exactly like an on-call engineer.  The
``ground_truth`` of each fault exists for the evaluation suite.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .world import World


def ramp(t: float, start: float | None, duration: float) -> float:
    """0 before `start`, linear to 1 over `duration` minutes, 1 afterwards."""
    if start is None or t < start:
        return 0.0
    if duration <= 0:
        return 1.0
    return min(1.0, (t - start) / duration)


@dataclass
class Effect:
    lat_by_hop: dict[str, float] = field(default_factory=dict)
    err_by_hop: dict[str, float] = field(default_factory=dict)
    traffic: float = 1.0
    qps_mult: float = 1.0
    cpu: float = 0.0
    memory: float | None = None
    restarts: int = 0
    connections: float | None = None

    def add_lat(self, hop: str, ms: float) -> None:
        if ms:
            self.lat_by_hop[hop] = self.lat_by_hop.get(hop, 0.0) + ms

    def add_err(self, hop: str, pct: float) -> None:
        if pct:
            self.err_by_hop[hop] = self.err_by_hop.get(hop, 0.0) + pct


@dataclass
class EdgeMod:
    lat_factor: float = 1.0
    err_factor: float = 1.0


@dataclass
class GroundTruth:
    category: str
    component: str
    summary: str
    change_id: str | None = None
    fixing_actions: tuple[str, ...] = ()


LogEvent = tuple[str, str, str]  # (component, level, message)


def _hex(rng: random.Random, n: int = 8) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


class FaultModel:
    kind: str = "fault"
    title: str = ""

    def __init__(self, **params: Any) -> None:
        self.params = params
        self.world: World | None = None
        self.t0: int = 0  # anomaly onset (minutes since window start)

    # -- lifecycle -------------------------------------------------------
    def setup(self, world: World, t0: int) -> None:
        self.world = world
        self.t0 = t0

    @property
    def root(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def alert_service(self) -> str:
        return self.root

    @property
    def alert_metric(self) -> str:
        return "error_rate_pct"

    def effects(self, t: int) -> tuple[dict[str, Effect], dict[tuple[str, str], EdgeMod]]:
        return {}, {}

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        return []

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        """React to a remediation action. Returns a note for the simulator log."""
        return None

    def ground_truth(self) -> GroundTruth:  # pragma: no cover - overridden
        raise NotImplementedError

    def describe(self) -> dict:
        return {"kind": self.kind, **{k: v for k, v in self.params.items()}}

    # helpers
    def _eff(self, store: dict[str, Effect], name: str) -> Effect:
        return store.setdefault(name, Effect())


# ---------------------------------------------------------------------------
# 1. A bad deploy: an N+1 query regression or a new exception
# ---------------------------------------------------------------------------
class BadDeploy(FaultModel):
    kind = "bad_deploy"

    def __init__(self, service: str, variant: str = "n_plus_one", datastore: str | None = None, **kw: Any):
        super().__init__(service=service, variant=variant, datastore=datastore, **kw)
        self.service = service
        self.variant = variant
        self.datastore = datastore
        self.t_deploy = 0
        self.t_rollback: int | None = None
        self.extra_replicas = 0.0
        self.change_id: str | None = None
        self.restart_at: list[int] = []

    @property
    def root(self) -> str:
        return self.service

    @property
    def alert_metric(self) -> str:
        return "latency_p99_ms" if self.variant == "n_plus_one" else "error_rate_pct"

    def setup(self, world: World, t0: int) -> None:
        super().setup(world, t0)
        comp = world.topology.get(self.service)
        self.from_version = world.versions[self.service]
        self.to_version = world.bump_version(self.from_version)
        self.t_deploy = t0 - 2
        self.change_id = world.add_change(
            self.t_deploy, "deploy", self.service,
            f"Deployed {self.service} {self.from_version} -> {self.to_version} (rolling, {comp.replicas} replicas)",
            author=world.pick_author(comp.team),
            details={"from_version": self.from_version, "to_version": self.to_version,
                     "commit": world.commit_sha(), "pipeline": f"deploy-{self.service}#{world.rng.randint(1200, 4800)}"},
        )
        world.versions[self.service] = self.to_version
        if self.variant == "n_plus_one" and not self.datastore:
            self.datastore = next(e.callee for e in world.topology.callees(self.service)
                                  if world.topology.get(e.callee).sql)

    def intensity(self, t: int) -> float:
        return ramp(t, self.t_deploy, 2) * (1 - ramp(t, self.t_rollback, 2))

    def effects(self, t: int):
        fx: dict[str, Effect] = {}
        i = self.intensity(t)
        if i <= 0:
            return fx, {}
        svc = self._eff(fx, self.service)
        if self.variant == "n_plus_one":
            db = self.world.topology.get(self.datastore)
            svc.add_lat(self.datastore, 2150 * i)
            svc.add_err(self.datastore, 4.6 * i)
            svc.cpu += 9 * i
            d = self._eff(fx, self.datastore)
            d.qps_mult = 1 + (9.5 + 2.0 * self.extra_replicas) * i
            d.add_lat("self", db.latency_ms * 2.3 * i)
            d.cpu += (36 + 8 * self.extra_replicas) * i
            d.connections = (db.base_connections or 50) * (1 + 0.45 * i)
        else:
            svc.add_err("self", 27 * i)
            svc.add_lat("self", 30 * i)
        if any(r == t for r in self.restart_at):
            svc.restarts = 1
            svc.add_err("self", 4.0)
        return fx, {}

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        out: list[LogEvent] = []
        w = self.world
        comp = w.topology.get(self.service)
        if t == self.t_deploy:
            out.append((self.service, "INFO",
                        f"rolling update started: {self.service} {self.from_version} -> {self.to_version} "
                        f"(maxSurge=1, maxUnavailable=0)"))
        if self.t_rollback is not None and t == self.t_rollback:
            out.append((self.service, "INFO",
                        f"rolling update started: {self.service} {self.to_version} -> {self.from_version} (rollback)"))
        i = self.intensity(t)
        if i <= 0:
            return out
        route = comp.routes[0] if comp.routes else "/"
        if self.variant == "n_plus_one":
            db = self.datastore
            table = {"orders-db": "order_items", "catalog-db": "product_variants",
                     "users-db": "user_roles"}.get(db, "items")
            fk = {"orders-db": "order_id", "catalog-db": "product_id", "users-db": "user_id"}.get(db, "parent_id")
            for _ in range(w.poisson(rng, 7 * i)):
                out.append((self.service, "WARN",
                            f"slow call to {db}: {rng.randint(780, 2400)}ms exceeded 500ms budget "
                            f"(endpoint {route})"))
            for _ in range(w.poisson(rng, 3.5 * i)):
                out.append((self.service, "ERROR",
                            f"request timeout after 2000ms waiting for {db} (endpoint {route}, "
                            f"trace_id={_hex(rng, 16)})"))
            for _ in range(w.poisson(rng, 9 * i)):
                out.append((db, "LOG",
                            f"duration: {rng.randint(120, 1900)}.{rng.randint(100, 999)} ms  statement: "
                            f"SELECT * FROM {table} WHERE {fk} = ${rng.randint(1, 9)} "
                            f"[application_name={self.service}@{self.to_version}]"))
            if rng.random() < 0.6 * i:
                out.append((db, "WARN",
                            f"query rate from {self.service} is {rng.randint(9, 12)}x its 1h average "
                            f"({rng.randint(6000, 9000)} queries/s)"))
        else:
            exc, frame = {
                "cart-service": ("panic: runtime error: invalid memory address or nil pointer dereference",
                                 "cart.(*Pricer).ApplyPromo(pricer.go:214)"),
                "checkout-service": ("NullPointerException", "PriceCalculator.applyDiscount(PriceCalculator.java:118)"),
                "catalog-service": ("IllegalStateException: availability badge missing",
                                    "BadgeRenderer.render(BadgeRenderer.kt:57)"),
                "search-service": ("KeyError: 'facets'", "search/api/handlers.py:211 in build_response"),
                "recommendation-service": ("ClassCastException", "RankingPipeline.score(RankingPipeline.java:301)"),
                "inventory-service": ("thread 'tokio-runtime-worker' panicked: called `Option::unwrap()` on a `None` value",
                                      "src/reservations.rs:88"),
                "payment-service": ("NullPointerException", "AuthorizationMapper.toRequest(AuthorizationMapper.java:66)"),
                "auth-service": ("panic: assignment to entry in nil map", "session.(*Store).Put(store.go:142)"),
            }.get(self.service, ("RuntimeError", "main"))
            for _ in range(w.poisson(rng, 11 * i)):
                out.append((self.service, "ERROR", f"{exc} at {frame} [build {self.to_version}]"))
            for _ in range(w.poisson(rng, 5 * i)):
                out.append((self.service, "ERROR",
                            f"500 Internal Server Error on {route} (trace_id={_hex(rng, 16)})"))
        return out

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        if action == "rollback_deploy" and params.get("service") == self.service:
            if params.get("to_version") in (None, self.from_version):
                self.t_rollback = t
                return "rollback removes the regression"
            return "rolled back to an unexpected version"
        if action == "restart_service" and params.get("service") == self.service:
            self.restart_at.append(t)
            return "restart does not change the deployed code"
        if action == "scale_service" and params.get("service") == self.service and self.variant == "n_plus_one":
            comp = self.world.topology.get(self.service)
            self.extra_replicas += max(0.0, params.get("replicas", comp.replicas) / comp.replicas - 1)
            return "more replicas send even more queries to the database"
        return None

    def ground_truth(self) -> GroundTruth:
        what = "an N+1 query regression" if self.variant == "n_plus_one" else "a new unhandled exception"
        return GroundTruth("bad_deploy", self.service,
                           f"{self.service} {self.to_version} introduced {what}", self.change_id, ("rollback_deploy",))


# ---------------------------------------------------------------------------
# 2. Database connection-pool exhaustion caused by a traffic surge (feature flag)
# ---------------------------------------------------------------------------
class PoolExhaustion(FaultModel):
    kind = "pool_exhaustion"

    def __init__(self, datastore: str = "orders-db", flag: str = "flash-sale-banner", surge: float | None = None,
                 **kw: Any):
        super().__init__(datastore=datastore, flag=flag, surge=surge, **kw)
        self.datastore = datastore
        self.flag = flag
        self.surge = surge or 0.0
        self.t_flag = 0
        self.t_flag_off: int | None = None
        self.capacity_changes: list[tuple[int, int]] = []
        self.caller_scale: list[tuple[int, float]] = []
        self.change_id: str | None = None

    @property
    def root(self) -> str:
        return self.datastore

    @property
    def alert_service(self) -> str:
        callers = [e.caller for e in self.world.topology.callers(self.datastore)]
        return max(callers, key=lambda c: (self.world.topology.get(c).revenue_critical,
                                           self.world.topology.get(c).throughput))

    def setup(self, world: World, t0: int) -> None:
        super().setup(world, t0)
        db = world.topology.get(self.datastore)
        if not self.surge:
            # big enough to push demand ~55% past the connection limit
            self.surge = round(1.55 * db.max_connections / (db.base_connections or 1), 2)
        self.t_flag = t0 - 3
        world.flags[self.flag] = {"enabled": False, "owner": "growth-marketing",
                                  "description": "Homepage flash-sale banner and deep links into checkout",
                                  "rollout_pct": 0}
        self.change_id = world.add_change(
            self.t_flag, "feature_flag", "web-frontend",
            f"Feature flag '{self.flag}' enabled for 100% of users",
            author="marketing-automation@lumen.shop",
            details={"flag": self.flag, "from": False, "to": True, "rollout_pct": 100,
                     "affects": sorted(world.topology.path_components(self.datastore) - {self.datastore})},
        )
        world.flags[self.flag].update(enabled=True, rollout_pct=100)

    def multiplier(self, t: int) -> float:
        return 1 + (self.surge - 1) * ramp(t, self.t_flag, 6) * (1 - ramp(t, self.t_flag_off, 3))

    def capacity(self, t: int) -> float:
        cap = float(self.world.topology.get(self.datastore).max_connections or 100)
        for at, value in self.capacity_changes:
            if t >= at:
                cap = float(value)
        return cap

    def demand(self, t: int) -> float:
        base = self.world.topology.get(self.datastore).base_connections or 50
        extra = sum(f for at, f in self.caller_scale if t >= at)
        return base * self.multiplier(t) * (1 + extra)

    def overload(self, t: int) -> float:
        d, cap = self.demand(t), self.capacity(t)
        return max(0.0, d - cap) / d if d > 0 else 0.0

    def effects(self, t: int):
        fx: dict[str, Effect] = {}
        m = self.multiplier(t)
        if m <= 1.0001 and not self.caller_scale:
            return fx, {}
        topo = self.world.topology
        for name in topo.path_components(self.datastore):
            self._eff(fx, name).traffic = m
        d = self._eff(fx, self.datastore)
        frac = (m - 1) / (self.surge - 1) if self.surge > 1 else 0
        d.connections = min(self.demand(t), self.capacity(t))
        d.cpu += 22 * frac
        d.add_lat("self", topo.get(self.datastore).latency_ms * 0.9 * frac)
        ov = self.overload(t)
        if ov > 0:
            for e in topo.callers(self.datastore):
                c = self._eff(fx, e.caller)
                c.add_err(self.datastore, 62 * ov)
                c.add_lat(self.datastore, min(5000.0, 14000 * ov))
        return fx, {}

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        out: list[LogEvent] = []
        w = self.world
        m = self.multiplier(t)
        if m > 1.25:
            rps = int(w.topology.get("web-frontend").throughput * m)
            if rng.random() < 0.7:
                out.append(("web-frontend", "INFO",
                            f"traffic surge: {rps} rps (+{int((m - 1) * 100)}% vs 1h avg), "
                            f"top referrer utm_campaign=flash-sale"))
        ov = self.overload(t)
        if ov <= 0:
            return out
        cap = int(self.capacity(t))
        for e in w.topology.callers(self.datastore):
            per_caller = max(10, int(cap * (0.6 if w.topology.get(e.caller).revenue_critical else 0.4)))
            for _ in range(w.poisson(rng, 16 * ov + 2)):
                out.append((e.caller, "ERROR",
                            f"could not acquire connection from pool within 5000ms (pool={self.datastore}, "
                            f"active={per_caller}/{per_caller}, waiting={rng.randint(20, 160)})"))
        for _ in range(w.poisson(rng, 10 * ov + 1)):
            out.append((self.datastore, "FATAL", f"sorry, too many clients already (max_connections={cap})"))
        return out

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        topo = self.world.topology
        if action == "increase_connection_pool" and params.get("datastore") == self.datastore:
            self.capacity_changes.append((t, int(params["max_connections"])))
            return "pool capacity raised"
        if action == "toggle_feature_flag" and params.get("flag") == self.flag and not params.get("enabled", True):
            self.t_flag_off = t
            return "surge traffic removed"
        if action == "scale_service":
            svc = params.get("service")
            if svc in {e.caller for e in topo.callers(self.datastore)}:
                comp = topo.get(svc)
                growth = max(0.0, params.get("replicas", comp.replicas) / comp.replicas - 1)
                self.caller_scale.append((t, 0.5 * growth))
                return "more replicas open more database connections"
        return None

    def ground_truth(self) -> GroundTruth:
        return GroundTruth("resource_exhaustion", self.datastore,
                           f"traffic surge from flag '{self.flag}' exhausted {self.datastore} connections",
                           self.change_id, ("increase_connection_pool", "toggle_feature_flag"))


# ---------------------------------------------------------------------------
# 3. A memory leak introduced by a deploy (OOMKilled restarts)
# ---------------------------------------------------------------------------
class MemoryLeak(FaultModel):
    kind = "memory_leak"

    def __init__(self, service: str = "recommendation-service", rate: float = 2.4, **kw: Any):
        super().__init__(service=service, rate=rate, **kw)
        self.service = service
        self.rate = rate
        self.t_deploy = 0
        self.t_rollback: int | None = None
        self.resets: list[int] = []
        self.scale_changes: list[tuple[int, float]] = []
        self._mem: list[float] = []
        self._oom: list[bool] = []
        self.change_id: str | None = None

    @property
    def root(self) -> str:
        return self.service

    @property
    def alert_service(self) -> str:
        callers = [e.caller for e in self.world.topology.callers(self.service)]
        return "api-gateway" if "api-gateway" in callers else callers[0]

    def setup(self, world: World, t0: int) -> None:
        super().setup(world, t0)
        comp = world.topology.get(self.service)
        self.from_version = world.versions[self.service]
        self.to_version = world.bump_version(self.from_version)
        base = comp.memory_pct
        # first OOM lands exactly on t0
        self.t_deploy = t0 - math.ceil((100 - base) / self.rate)
        self.change_id = world.add_change(
            self.t_deploy, "deploy", self.service,
            f"Deployed {self.service} {self.from_version} -> {self.to_version} (rolling, {comp.replicas} replicas)",
            author=world.pick_author(comp.team),
            details={"from_version": self.from_version, "to_version": self.to_version,
                     "commit": world.commit_sha(),
                     "notes": "Cache recommendation candidates per session for faster homepage loads"},
        )
        world.versions[self.service] = self.to_version

    def _leak_rate(self, t: int) -> float:
        factor = 1.0
        for at, f in self.scale_changes:
            if t >= at:
                factor = f
        return self.rate * factor

    def _extend(self, t: int) -> None:
        base = self.world.topology.get(self.service).memory_pct
        while len(self._mem) <= t:
            i = len(self._mem)
            prev = self._mem[-1] if self._mem else base
            oom = False
            if i in self.resets or (self.t_rollback is not None and i == self.t_rollback):
                mem = base - 2
            elif i < self.t_deploy or (self.t_rollback is not None and i > self.t_rollback):
                mem = base
            else:
                mem = prev + self._leak_rate(i)
                if mem >= 100:
                    oom, mem = True, base - 3
            self._mem.append(mem)
            self._oom.append(oom)

    def _truncate(self, t: int) -> None:
        del self._mem[t:]
        del self._oom[t:]

    def memory(self, t: int) -> float:
        self._extend(t)
        return self._mem[t]

    def oom(self, t: int) -> bool:
        self._extend(t)
        return self._oom[t]

    def effects(self, t: int):
        fx: dict[str, Effect] = {}
        if t < self.t_deploy:
            return fx, {}
        e = self._eff(fx, self.service)
        mem = self.memory(t)
        e.memory = mem
        e.add_lat("self", 1300 * max(0.0, mem - 80) / 20)
        e.cpu += 28 * max(0.0, mem - 75) / 25
        if self.oom(t):
            e.restarts = 1
            e.add_err("self", 31)
        elif t >= 1 and self.oom(t - 1):
            e.add_err("self", 12)
        elif mem > 92:
            e.add_err("self", 1.5)
        return fx, {}

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        out: list[LogEvent] = []
        if t == self.t_deploy:
            out.append((self.service, "INFO",
                        f"rolling update started: {self.service} {self.from_version} -> {self.to_version}"))
        if t < self.t_deploy:
            return out
        mem = self.memory(t)
        if t >= self.t_deploy and (t - self.t_deploy) % 4 == 0 and (self.t_rollback is None or t < self.t_rollback):
            entries = int((t - self.t_deploy + 1) * 52_000 * self._leak_rate(t) / self.rate)
            out.append((self.service, "INFO",
                        f"recommendation candidate cache size={entries} entries (eviction policy: none)"))
        if mem > 80:
            for _ in range(self.world.poisson(rng, 3)):
                out.append((self.service, "WARN",
                            f"GC pause {rng.randint(400, 2600)}ms (G1 Evacuation Pause), heap {int(mem)}% used"))
        if self.oom(t):
            restarts = sum(1 for i in range(t + 1) if self.oom(i))
            out.append((self.service, "ERROR",
                        "java.lang.OutOfMemoryError: Java heap space at "
                        "java.util.HashMap.resize(HashMap.java:704)"))
            out.append((self.service, "WARN",
                        f"OOMKilled: container {self.service} in pod {self.service}-{_hex(rng, 5)} exceeded "
                        f"memory limit 2Gi (restart #{restarts})"))
            for e in self.world.topology.callers(self.service):
                for _ in range(self.world.poisson(rng, 9)):
                    out.append((e.caller, "ERROR",
                                f"upstream {self.service} returned 502 (connection reset by peer) "
                                f"route=/api/recommendations"))
        return out

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        if params.get("service") != self.service:
            return None
        if action == "rollback_deploy":
            self._truncate(t)
            self.t_rollback = t
            return "rollback removes the leaking cache"
        if action == "restart_service":
            self._truncate(t)
            self.resets.append(t)
            return "restart frees memory but the leak continues"
        if action == "scale_service":
            comp = self.world.topology.get(self.service)
            replicas = max(1, int(params.get("replicas", comp.replicas)))
            self._truncate(t)
            self.scale_changes.append((t, comp.replicas / replicas))
            return "more replicas slow the per-pod leak"
        return None

    def ground_truth(self) -> GroundTruth:
        return GroundTruth("memory_leak", self.service,
                           f"{self.service} {self.to_version} leaks memory (unbounded cache) causing OOMKills",
                           self.change_id, ("rollback_deploy",))


# ---------------------------------------------------------------------------
# 4. Third-party outage (payment provider)
# ---------------------------------------------------------------------------
class ExternalOutage(FaultModel):
    kind = "external_outage"

    def __init__(self, provider: str = "payrail-api", via: str = "payment-service",
                 failover_flag: str = "payments-failover-backup-provider", **kw: Any):
        super().__init__(provider=provider, via=via, failover_flag=failover_flag, **kw)
        self.provider = provider
        self.via = via
        self.flag = failover_flag
        self.t_start = 0
        self.t_breaker: int | None = None
        self.t_failover: int | None = None

    @property
    def root(self) -> str:
        return self.provider

    @property
    def alert_service(self) -> str:
        callers = [e.caller for e in self.world.topology.callers(self.via)]
        return callers[0] if callers else self.via

    def setup(self, world: World, t0: int) -> None:
        super().setup(world, t0)
        self.t_start = t0
        world.flags[self.flag] = {"enabled": False, "owner": "payments",
                                  "description": "Route card authorisations to the backup acquirer (slower, +0.4% fees)",
                                  "rollout_pct": 0}
        world.vendor_incidents.append({
            "vendor": "PayRail", "component": self.provider, "t": t0 + 1, "status": "degraded_performance",
            "title": "Elevated error rates for card authorizations in us-east",
            "message": "We are investigating elevated 503 responses and timeouts on /v2/authorize.",
        })

    def intensity(self, t: int) -> float:
        return ramp(t, self.t_start, 2)

    def effects(self, t: int):
        fx: dict[str, Effect] = {}
        mods: dict[tuple[str, str], EdgeMod] = {}
        i = self.intensity(t)
        if i <= 0:
            return fx, mods
        p = self._eff(fx, self.provider)
        p.add_err("self", 46 * i)
        p.add_lat("self", 4300 * i)
        if self.t_failover is not None and t >= self.t_failover:
            mods[(self.via, self.provider)] = EdgeMod(lat_factor=0.0, err_factor=0.0)
            p.traffic = 0.02
            v = self._eff(fx, self.via)
            v.add_lat("backup-acquirer", 140)
            v.add_err("backup-acquirer", 0.3)
        elif self.t_breaker is not None and t >= self.t_breaker:
            mods[(self.via, self.provider)] = EdgeMod(lat_factor=0.02, err_factor=1.0)
        return fx, mods

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        out: list[LogEvent] = []
        i = self.intensity(t)
        if i <= 0:
            return out
        w = self.world
        failover = self.t_failover is not None and t >= self.t_failover
        breaker = self.t_breaker is not None and t >= self.t_breaker
        if failover:
            if rng.random() < 0.8:
                out.append((self.via, "INFO", "authorization routed to backup acquirer (failover flag active)"))
            return out
        for _ in range(w.poisson(rng, 13 * i)):
            if breaker:
                out.append((self.via, "WARN", f"circuit breaker {self.provider} OPEN: failing fast "
                                              f"(request_id={_hex(rng, 12)})"))
            else:
                out.append((self.via, "ERROR",
                            f"PayRail authorize failed: 503 Service Unavailable "
                            f"(upstream_request_id={_hex(rng, 12)}) after {rng.randint(900, 4900)}ms"))
        if not breaker:
            for _ in range(w.poisson(rng, 4 * i)):
                out.append((self.via, "WARN",
                            f"PayRail p99 latency {rng.randint(3800, 5200)}ms exceeds timeout budget 4000ms"))
        for e in w.topology.callers(self.via):
            for _ in range(w.poisson(rng, 6 * i)):
                out.append((e.caller, "ERROR",
                            f"payment step failed for order ord_{_hex(rng, 10)}: upstream {self.via} returned 502"))
        return out

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        if action == "enable_circuit_breaker" and params.get("service") == self.via \
                and params.get("dependency") == self.provider:
            self.t_breaker = t
            return "breaker fails fast: latency recovers, errors remain"
        if action == "toggle_feature_flag" and params.get("flag") == self.flag and params.get("enabled", False):
            self.t_failover = t
            return "traffic moved to the backup acquirer"
        return None

    def ground_truth(self) -> GroundTruth:
        return GroundTruth("external_dependency", self.provider,
                           "PayRail (third-party acquirer) is returning 503s and timing out", None,
                           ("toggle_feature_flag",))


# ---------------------------------------------------------------------------
# 5. Expired mTLS client certificate
# ---------------------------------------------------------------------------
class CertExpiry(FaultModel):
    kind = "cert_expiry"

    def __init__(self, service: str = "auth-service", datastore: str | None = None, **kw: Any):
        super().__init__(service=service, datastore=datastore, **kw)
        self.service = service
        self.datastore = datastore
        self.t_rotated: int | None = None
        self.cert_name = ""

    @property
    def root(self) -> str:
        return self.service

    def setup(self, world: World, t0: int) -> None:
        super().setup(world, t0)
        topo = world.topology
        if not self.datastore:
            self.datastore = next(e.callee for e in topo.callees(self.service) if topo.get(e.callee).sql)
        self.cert_name = f"{self.datastore}-client-{self.service.removesuffix('-service')}"
        world.set_certificate(self.cert_name, owner=self.service, used_for=f"mTLS client auth {self.service} -> {self.datastore}",
                              not_after_t=t0, issuer="Lumen Internal CA G2")

    def active(self, t: int) -> bool:
        return t >= self.t0 and not (self.t_rotated is not None and t >= self.t_rotated + 1)

    def effects(self, t: int):
        fx: dict[str, Effect] = {}
        if not self.active(t):
            return fx, {}
        s = self._eff(fx, self.service)
        s.add_err(self.datastore, 93)
        db = self.world.topology.get(self.datastore)
        d = self._eff(fx, self.datastore)
        d.connections = (db.base_connections or 40) * 0.12
        d.traffic = 0.15
        return fx, {}

    def logs(self, t: int, rng: random.Random) -> list[LogEvent]:
        if not self.active(t):
            return []
        w = self.world
        out: list[LogEvent] = []
        not_after = w.iso(self.t0)
        now = w.iso(t, rng.randint(0, 59))
        for _ in range(w.poisson(rng, 18)):
            out.append((self.service, "ERROR",
                        f"tls handshake with {self.datastore}:5432 failed: x509: certificate has expired or is "
                        f"not yet valid: current time {now} is after {not_after}"))
        for _ in range(w.poisson(rng, 6)):
            route = w.topology.get(self.service).routes[0]
            out.append((self.service, "ERROR", f"{route} 503: database unavailable ({self.datastore})"))
        for _ in range(w.poisson(rng, 5)):
            out.append((self.datastore, "LOG",
                        f"could not accept SSL connection: sslv3 alert certificate expired "
                        f"(client={self.service})"))
        for e in w.topology.callers(self.service):
            for _ in range(w.poisson(rng, 5)):
                out.append((e.caller, "ERROR", f"upstream {self.service} returned 503 route=/api/login"))
        return out

    def on_action(self, t: int, action: str, params: dict) -> str | None:
        if action == "rotate_certificate" and params.get("certificate") == self.cert_name:
            self.t_rotated = t
            return "new certificate loaded"
        return None

    def ground_truth(self) -> GroundTruth:
        return GroundTruth("certificate_expiry", self.service,
                           f"mTLS client certificate '{self.cert_name}' used by {self.service} expired", None,
                           ("rotate_certificate",))


FAULT_TYPES: dict[str, type[FaultModel]] = {
    BadDeploy.kind: BadDeploy,
    PoolExhaustion.kind: PoolExhaustion,
    MemoryLeak.kind: MemoryLeak,
    ExternalOutage.kind: ExternalOutage,
    CertExpiry.kind: CertExpiry,
}


def build_fault(spec: dict) -> FaultModel:
    spec = dict(spec)
    kind = spec.pop("kind")
    return FAULT_TYPES[kind](**spec)
