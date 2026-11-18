"""The simulated production estate: "Lumen Shop", a small e-commerce platform.

Eleven services, six datastores / infra components and one third-party payment
API.  Every component has a baseline (what "healthy" looks like), the metrics it
exposes and, for user-facing services, an SLO that alerts are evaluated against.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

Kind = Literal["service", "datastore", "infra", "external"]

# Metric names exposed per component kind.  Units are encoded in the name so an
# LLM (or a human) never has to guess them.
SERVICE_METRICS = ("latency_p99_ms", "error_rate_pct", "throughput_rps", "cpu_pct", "memory_pct", "restarts")
SQL_METRICS = ("query_latency_p95_ms", "throughput_qps", "connections", "cpu_pct")
CACHE_METRICS = ("latency_p99_ms", "throughput_ops", "cpu_pct", "memory_pct")
SEARCH_METRICS = ("query_latency_p95_ms", "throughput_qps", "cpu_pct")
BUS_METRICS = ("consumer_lag", "throughput_msgs", "cpu_pct")
EXTERNAL_METRICS = ("latency_p99_ms", "error_rate_pct", "throughput_rps")


@dataclass(frozen=True)
class Component:
    name: str
    kind: Kind
    tier: str  # edge | core | async | data | external
    team: str
    tech: str
    description: str
    metrics: tuple[str, ...]
    latency_ms: float  # p99 (services, cache, external) or p95 query latency (databases)
    throughput: float  # rps / qps / ops / msgs per second
    error_pct: float = 0.0
    cpu_pct: float = 30.0
    memory_pct: float = 50.0
    replicas: int = 0
    version: str | None = None
    previous_version: str | None = None
    slo_latency_ms: float | None = None
    slo_error_pct: float | None = None
    max_connections: int | None = None
    base_connections: float | None = None
    revenue_critical: bool = False
    routes: tuple[str, ...] = ()
    sql: bool = False

    @property
    def has_slo(self) -> bool:
        return self.slo_latency_ms is not None or self.slo_error_pct is not None


@dataclass(frozen=True)
class Edge:
    caller: str
    callee: str
    share: float  # fraction of the caller's requests that (synchronously) call the callee
    critical: bool  # does a failing callee fail the caller's request?
    timeout_ms: int = 3000


def _svc(name, *, tier="core", team, tech, description, latency, rps, error=0.15, cpu=32.0, mem=48.0,
         replicas=3, version, previous, slo_latency, slo_error=1.0, revenue=False, routes=()):
    return Component(
        name=name, kind="service", tier=tier, team=team, tech=tech, description=description,
        metrics=SERVICE_METRICS, latency_ms=latency, throughput=rps, error_pct=error, cpu_pct=cpu,
        memory_pct=mem, replicas=replicas, version=version, previous_version=previous,
        slo_latency_ms=slo_latency, slo_error_pct=slo_error, revenue_critical=revenue, routes=tuple(routes),
    )


def _db(name, *, team, description, latency, qps, conns, max_conns, cpu=28.0):
    return Component(
        name=name, kind="datastore", tier="data", team=team, tech="PostgreSQL 16", description=description,
        metrics=SQL_METRICS, latency_ms=latency, throughput=qps, cpu_pct=cpu, memory_pct=62.0,
        max_connections=max_conns, base_connections=conns, sql=True,
    )


COMPONENTS: tuple[Component, ...] = (
    _svc("web-frontend", tier="edge", team="storefront", tech="Next.js / Node 22",
         description="Server-side rendered storefront; every customer page view starts here.",
         latency=240, rps=880, error=0.12, cpu=38, mem=52, replicas=6, version="7.4.2", previous="7.4.1",
         slo_latency=800, slo_error=1.0, revenue=True, routes=("GET /", "GET /product/:id", "GET /checkout")),
    _svc("api-gateway", tier="edge", team="platform", tech="Envoy + Go control plane",
         description="Edge API gateway: authn, routing, rate limiting for all /api traffic.",
         latency=150, rps=1420, error=0.10, cpu=41, mem=44, replicas=6, version="3.21.0", previous="3.20.4",
         slo_latency=600, slo_error=1.0, revenue=True,
         routes=("POST /api/login", "GET /api/catalog", "GET /api/search", "GET /api/recommendations",
                 "POST /api/cart/items", "POST /api/checkout/confirm")),
    _svc("auth-service", team="identity", tech="Go 1.23",
         description="Login, sessions and token issuance. Talks to users-db over mTLS.",
         latency=95, rps=260, error=0.08, cpu=24, mem=40, replicas=3, version="5.9.3", previous="5.9.2",
         slo_latency=400, slo_error=1.0, revenue=True, routes=("POST /login", "POST /token/refresh")),
    _svc("catalog-service", team="catalog", tech="Kotlin / Spring Boot 3",
         description="Product catalog reads, pricing and availability badges.",
         latency=140, rps=520, error=0.10, cpu=36, mem=55, replicas=4, version="12.3.0", previous="12.2.7",
         slo_latency=500, slo_error=1.0, routes=("GET /products", "GET /products/:id")),
    _svc("search-service", team="catalog", tech="Python 3.12 / FastAPI",
         description="Full-text product search backed by search-index.",
         latency=170, rps=210, error=0.12, cpu=34, mem=50, replicas=3, version="3.8.0", previous="3.7.9",
         slo_latency=700, slo_error=2.0, routes=("GET /search",)),
    _svc("recommendation-service", team="personalization", tech="Java 21 / Micronaut",
         description="'You may also like' and homepage recommendations with an in-memory model cache.",
         latency=210, rps=300, error=0.15, cpu=40, mem=45, replicas=3, version="5.1.3", previous="5.1.2",
         slo_latency=900, slo_error=2.0, routes=("GET /recommendations",)),
    _svc("cart-service", team="checkout", tech="Go 1.23",
         description="Shopping cart state, stored in redis-cache.",
         latency=70, rps=340, error=0.08, cpu=22, mem=38, replicas=3, version="4.2.1", previous="4.2.0",
         slo_latency=300, slo_error=1.0, revenue=True, routes=("POST /cart/items", "GET /cart")),
    _svc("checkout-service", team="checkout", tech="Java 21 / Spring Boot 3",
         description="Order placement: validates the cart, reserves stock, charges the card, writes the order.",
         latency=420, rps=85, error=0.20, cpu=35, mem=58, replicas=4, version="2.13.2", previous="2.13.1",
         slo_latency=1200, slo_error=1.0, revenue=True, routes=("POST /checkout/confirm", "GET /checkout/summary")),
    _svc("inventory-service", team="fulfilment", tech="Rust / Axum",
         description="Stock levels and reservations.",
         latency=60, rps=120, error=0.06, cpu=18, mem=30, replicas=3, version="1.18.4", previous="1.18.3",
         slo_latency=300, slo_error=1.0, routes=("POST /reservations", "GET /stock/:sku")),
    _svc("payment-service", team="payments", tech="Java 21 / Quarkus",
         description="Card authorisation through PayRail (primary) or a backup acquirer.",
         latency=480, rps=70, error=0.25, cpu=26, mem=46, replicas=3, version="8.0.6", previous="8.0.5",
         slo_latency=2000, slo_error=1.5, revenue=True, routes=("POST /payments/authorize",)),
    _svc("notification-service", tier="async", team="comms", tech="Node 22",
         description="Order confirmation emails and push notifications (consumes event-bus).",
         latency=55, rps=40, error=0.05, cpu=15, mem=35, replicas=2, version="2.6.0", previous="2.5.9",
         slo_latency=None, slo_error=None, routes=("consume orders.events",)),
    _db("users-db", team="identity", description="Accounts and credentials (PostgreSQL, mTLS only).",
        latency=6.0, qps=900, conns=55, max_conns=200),
    _db("catalog-db", team="catalog", description="Products, prices and availability (PostgreSQL).",
        latency=8.0, qps=1650, conns=85, max_conns=300),
    _db("orders-db", team="checkout", description="Orders, order items and stock reservations (PostgreSQL).",
        latency=9.0, qps=720, conns=118, max_conns=200),
    Component("redis-cache", "datastore", "data", "platform", "Redis 7 cluster",
              "Shared cache: carts, sessions, catalog fragments.", CACHE_METRICS,
              latency_ms=1.6, throughput=12500, cpu_pct=21, memory_pct=58),
    Component("search-index", "datastore", "data", "catalog", "OpenSearch 2",
              "Product search index.", SEARCH_METRICS, latency_ms=24, throughput=230, cpu_pct=31, memory_pct=66),
    Component("event-bus", "infra", "data", "platform", "Kafka 3.7",
              "Order and notification events.", BUS_METRICS, latency_ms=4, throughput=160, cpu_pct=19),
    Component("payrail-api", "external", "external", "payments (vendor: PayRail)", "Third-party REST API",
              "Primary card acquirer. Client-side metrics are measured by payment-service.",
              EXTERNAL_METRICS, latency_ms=330, throughput=70, error_pct=0.2),
)

EDGES: tuple[Edge, ...] = (
    Edge("web-frontend", "api-gateway", 1.0, True, 5000),
    Edge("api-gateway", "auth-service", 0.18, True),
    Edge("api-gateway", "catalog-service", 0.36, True),
    Edge("api-gateway", "search-service", 0.15, True),
    Edge("api-gateway", "recommendation-service", 0.20, True),
    Edge("api-gateway", "cart-service", 0.16, True),
    Edge("api-gateway", "checkout-service", 0.06, True, 5000),
    Edge("auth-service", "users-db", 1.0, True, 2000),
    Edge("catalog-service", "catalog-db", 0.9, True, 2000),
    Edge("catalog-service", "redis-cache", 1.0, False, 200),
    Edge("search-service", "search-index", 1.0, True, 2000),
    Edge("recommendation-service", "catalog-service", 0.5, True),
    Edge("recommendation-service", "redis-cache", 1.0, False, 200),
    Edge("cart-service", "redis-cache", 1.0, True, 500),
    Edge("checkout-service", "cart-service", 1.0, True),
    Edge("checkout-service", "inventory-service", 1.0, True),
    Edge("checkout-service", "payment-service", 1.0, True, 5000),
    Edge("checkout-service", "orders-db", 1.0, True, 2000),
    Edge("checkout-service", "event-bus", 1.0, False, 500),
    Edge("inventory-service", "orders-db", 1.0, True, 2000),
    Edge("payment-service", "payrail-api", 1.0, True, 5000),
    Edge("notification-service", "event-bus", 1.0, True),
)


@dataclass
class Topology:
    components: dict[str, Component] = field(default_factory=lambda: {c.name: c for c in COMPONENTS})
    edges: tuple[Edge, ...] = EDGES

    def __contains__(self, name: str) -> bool:
        return name in self.components

    def get(self, name: str) -> Component:
        try:
            return self.components[name]
        except KeyError:
            raise KeyError(f"unknown component '{name}'. Known: {', '.join(sorted(self.components))}") from None

    def callees(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.caller == name]

    def callers(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.callee == name]

    def edge(self, caller: str, callee: str) -> Edge | None:
        return next((e for e in self.edges if e.caller == caller and e.callee == callee), None)

    @cached_property
    def leaves_first(self) -> list[str]:
        """Topological order with dependencies before their callers."""
        order: list[str] = []
        visiting: set[str] = set()

        def visit(n: str) -> None:
            if n in order:
                return
            visiting.add(n)
            for e in self.callees(n):
                if e.callee not in visiting:
                    visit(e.callee)
            visiting.discard(n)
            order.append(n)

        for n in self.components:
            visit(n)
        return order

    def upstream(self, name: str) -> list[str]:
        """Every component that (transitively) depends on `name` -- its blast radius."""
        seen: list[str] = []
        queue = deque([name])
        while queue:
            cur = queue.popleft()
            for e in self.callers(cur):
                if e.caller not in seen:
                    seen.append(e.caller)
                    queue.append(e.caller)
        return seen

    def downstream(self, name: str) -> list[str]:
        seen: list[str] = []
        queue = deque([name])
        while queue:
            cur = queue.popleft()
            for e in self.callees(cur):
                if e.callee not in seen:
                    seen.append(e.callee)
                    queue.append(e.callee)
        return seen

    def path_components(self, target: str) -> set[str]:
        """All components on any call path from the edge to `target` (inclusive)."""
        return {target, *self.upstream(target)}

    def as_dict(self) -> dict:
        return {
            "components": [
                {
                    "name": c.name, "kind": c.kind, "tier": c.tier, "team": c.team, "tech": c.tech,
                    "description": c.description, "version": c.version, "replicas": c.replicas,
                    "slo": {"latency_p99_ms": c.slo_latency_ms, "error_rate_pct": c.slo_error_pct} if c.has_slo else None,
                    "revenue_critical": c.revenue_critical,
                }
                for c in self.components.values()
            ],
            "edges": [{"caller": e.caller, "callee": e.callee, "share": e.share, "critical": e.critical}
                      for e in self.edges],
        }
