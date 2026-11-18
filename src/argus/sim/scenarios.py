"""Curated incident scenarios plus a "chaos" generator for random, unseen faults."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

from .world import ScenarioSpec


@dataclass(frozen=True)
class ScenarioCard:
    id: str
    title: str
    teaser: str
    fault: dict
    seed: int
    difficulty: str
    icon: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def spec(self, seed: int | None = None) -> ScenarioSpec:
        return ScenarioSpec(id=self.id, title=self.title, fault=dict(self.fault),
                            seed=self.seed if seed is None else seed, description=self.teaser)

    def as_dict(self) -> dict:
        return asdict(self)


CATALOG: tuple[ScenarioCard, ...] = (
    ScenarioCard(
        "checkout-deploy-regression", "Checkout slows to a crawl after a deploy",
        "p99 latency on checkout-service jumps past its 1.2 s SLO minutes after a release.",
        {"kind": "bad_deploy", "service": "checkout-service", "variant": "n_plus_one", "datastore": "orders-db"},
        seed=11, difficulty="Medium", icon="rocket", tags=("deploy", "database", "latency"),
    ),
    ScenarioCard(
        "flash-sale-pool-exhaustion", "A flash sale exhausts the orders database",
        "Checkout errors climb while marketing celebrates record traffic.",
        {"kind": "pool_exhaustion", "datastore": "orders-db", "flag": "flash-sale-banner"},
        seed=23, difficulty="Hard", icon="flame", tags=("traffic", "database", "feature flag"),
    ),
    ScenarioCard(
        "recommendations-memory-leak", "Homepage 502s from a crash-looping service",
        "The API gateway reports bursts of 502s on the homepage recommendations route.",
        {"kind": "memory_leak", "service": "recommendation-service"},
        seed=5, difficulty="Hard", icon="memory", tags=("memory", "OOMKilled", "deploy"),
    ),
    ScenarioCard(
        "payment-provider-outage", "Card payments failing at a third-party provider",
        "Checkout error rate spikes; nothing was deployed in the last hour.",
        {"kind": "external_outage", "provider": "payrail-api", "via": "payment-service"},
        seed=31, difficulty="Medium", icon="card", tags=("third-party", "payments", "failover"),
    ),
    ScenarioCard(
        "auth-cert-expiry", "Nobody can log in",
        "auth-service error rate goes from 0.1% to 90% in a single minute.",
        {"kind": "cert_expiry", "service": "auth-service"},
        seed=17, difficulty="Easy", icon="lock", tags=("TLS", "certificates", "identity"),
    ),
    ScenarioCard(
        "cart-bad-release", "Add-to-cart failing after a release",
        "cart-service starts returning HTTP 500 for one in four requests.",
        {"kind": "bad_deploy", "service": "cart-service", "variant": "exception"},
        seed=3, difficulty="Easy", icon="cart", tags=("deploy", "exception"),
    ),
)

BY_ID = {c.id: c for c in CATALOG}

_N_PLUS_ONE = (("checkout-service", "orders-db"), ("catalog-service", "catalog-db"),
               ("auth-service", "users-db"), ("inventory-service", "orders-db"))
_EXCEPTION = ("auth-service", "catalog-service", "search-service", "recommendation-service", "cart-service",
              "checkout-service", "inventory-service", "payment-service")
_POOL = ("orders-db", "catalog-db", "users-db")
_LEAK = ("recommendation-service", "search-service", "catalog-service", "cart-service")
_CERT = (("auth-service", "users-db"), ("catalog-service", "catalog-db"), ("checkout-service", "orders-db"),
         ("inventory-service", "orders-db"))


def chaos_fault(seed: int) -> dict:
    """A random fault with a random (valid) target -- the agent has never seen it."""
    rng = random.Random(f"chaos:{seed}")
    kind = rng.choice(("bad_deploy", "bad_deploy", "pool_exhaustion", "memory_leak", "external_outage", "cert_expiry"))
    if kind == "bad_deploy":
        if rng.random() < 0.5:
            service, db = rng.choice(_N_PLUS_ONE)
            return {"kind": kind, "service": service, "variant": "n_plus_one", "datastore": db}
        return {"kind": kind, "service": rng.choice(_EXCEPTION), "variant": "exception"}
    if kind == "pool_exhaustion":
        return {"kind": kind, "datastore": rng.choice(_POOL), "flag": "flash-sale-banner"}
    if kind == "memory_leak":
        return {"kind": kind, "service": rng.choice(_LEAK), "rate": round(rng.uniform(1.8, 3.0), 2)}
    if kind == "external_outage":
        return {"kind": kind, "provider": "payrail-api", "via": "payment-service"}
    service, db = rng.choice(_CERT)
    return {"kind": kind, "service": service, "datastore": db}


def resolve(scenario_id: str, seed: int | None = None) -> ScenarioSpec:
    """Scenario id -> spec.  ``chaos`` (optionally ``chaos-<seed>``) builds a random fault."""
    if scenario_id.startswith("chaos"):
        if seed is None:
            suffix = scenario_id.removeprefix("chaos").lstrip("-")
            seed = int(suffix) if suffix.isdigit() else random.randint(1, 10**6)
        return ScenarioSpec(id=f"chaos-{seed}", title="Chaos: an unknown fault", fault=chaos_fault(seed),
                            seed=seed, description="A randomly injected fault. Argus has to find it from scratch.")
    try:
        return BY_ID[scenario_id].spec(seed)
    except KeyError:
        raise KeyError(f"unknown scenario '{scenario_id}'. Known: {', '.join(BY_ID)} or chaos") from None
