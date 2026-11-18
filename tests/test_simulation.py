"""The simulator is the ground the agent stands on: it must be deterministic, realistic and causal."""

import pytest

from argus.sim import BY_ID, CATALOG, World, resolve
from conftest import ANCHOR

FIX = {
    "bad_deploy": lambda f: ("rollback_deploy", {"service": f.root}),
    "pool_exhaustion": lambda f: ("increase_connection_pool", {"datastore": f.root, "max_connections": 2 * f.capacity(0)}),
    "memory_leak": lambda f: ("rollback_deploy", {"service": f.root}),
    "external_outage": lambda f: ("toggle_feature_flag", {"flag": f.flag, "enabled": True}),
    "cert_expiry": lambda f: ("rotate_certificate", {"certificate": f.cert_name}),
}


def test_worlds_are_deterministic():
    a, b = World(BY_ID["flash-sale-pool-exhaustion"].spec(), ANCHOR), World(BY_ID["flash-sale-pool-exhaustion"].spec(), ANCHOR)
    assert a.alert == b.alert
    assert a.series("checkout-service", "error_rate_pct", 0) == b.series("checkout-service", "error_rate_pct", 0)
    assert [l.message for l in a.logs("orders-db", 80)] == [l.message for l in b.logs("orders-db", 80)]
    assert [c.id for c in a.changes] == [c.id for c in b.changes]


@pytest.mark.parametrize("card", CATALOG, ids=lambda c: c.id)
def test_alert_fires_and_textbook_fix_recovers(card):
    w = World(card.spec(), ANCHOR)
    svc, metric, threshold = w.alert["service"], w.alert["metric"], w.alert["threshold"]
    assert w.alert["fired_t"] < w.clock
    assert max(v for _, v in w.series(svc, metric, w.clock - 6)) > threshold
    action, params = FIX[w.fault.kind](w.fault)
    assert w.apply_action(action, params)["ok"]
    w.advance(10)
    tail = [v for _, v in w.series(svc, metric, w.clock - 4)]
    assert max(tail) <= threshold, f"{card.id}: {metric} still {tail} after {action}"


def test_wrong_fix_does_not_help():
    w = World(BY_ID["auth-cert-expiry"].spec(), ANCHOR)
    assert w.apply_action("restart_service", {"service": "auth-service"})["ok"]
    w.advance(8)
    assert min(v for _, v in w.series("auth-service", "error_rate_pct", w.clock - 4)) > 50


def test_actions_never_rewrite_history():
    w = World(BY_ID["payment-provider-outage"].spec(), ANCHOR)
    t = w.clock
    w.apply_action("enable_circuit_breaker", {"service": "payment-service", "dependency": "payrail-api"})
    w.advance(6)
    before = [v for _, v in w.series("checkout-service", "latency_p99_ms", t - 5, t)]
    after = [v for _, v in w.series("checkout-service", "latency_p99_ms", t + 2, w.clock)]
    assert min(before[-3:]) > 3000 and max(after) < 1000


def test_scaling_callers_makes_pool_exhaustion_worse():
    w = World(BY_ID["flash-sale-pool-exhaustion"].spec(), ANCHOR)
    w.advance(5)
    base = w.fault.overload(w.clock)
    w.apply_action("scale_service", {"service": "checkout-service", "replicas": 8})
    assert w.fault.overload(w.clock + 1) > base


def test_restart_only_buys_time_for_a_memory_leak():
    w = World(BY_ID["recommendations-memory-leak"].spec(), ANCHOR)
    w.apply_action("restart_service", {"service": "recommendation-service"})
    w.advance(10)
    mem = [v for _, v in w.series("recommendation-service", "memory_pct", w.clock - 6)]
    assert mem[-1] - mem[0] > 8  # still leaking


def test_control_plane_validates_actions():
    w = World(BY_ID["cart-bad-release"].spec(), ANCHOR)
    assert not w.apply_action("rollback_deploy", {"service": "orders-db"})["ok"]
    assert not w.apply_action("toggle_feature_flag", {"flag": "nope", "enabled": True})["ok"]
    assert not w.apply_action("increase_connection_pool", {"datastore": "orders-db", "max_connections": 10})["ok"]
    assert not w.apply_action("drop_database", {})["ok"]
    ok = w.apply_action("rollback_deploy", {"service": "cart-service"}, action_id="x1")
    again = w.apply_action("rollback_deploy", {"service": "cart-service"}, action_id="x1")
    assert ok["ok"] and again["change_id"] == ok["change_id"]  # idempotent


@pytest.mark.parametrize("seed", range(1, 41))
def test_chaos_faults_are_well_formed(seed):
    spec = resolve("chaos", seed)
    w = World(spec, ANCHOR)
    assert w.alert["fired_t"] >= spec.onset - 8
    assert w.fault.ground_truth().component in w.topology
