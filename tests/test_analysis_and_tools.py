from argus.analysis import classify_signature, correlate_change, detect_anomaly, extract_refs, to_template
from argus.knowledge import RunbookRetriever, load_runbooks
from argus.sim import BY_ID, World
from argus.tools import ANALYST_TOOLS, build_tools
from conftest import ANCHOR


def series(values, start=0):
    return list(enumerate(values, start))


def test_detects_step_ramp_spike_and_nothing():
    flat = [100 + (i % 3) for i in range(60)]
    step = detect_anomaly(series(flat + [400] * 10), "latency_p99_ms")
    assert step and step.pattern == "step" and step.onset_t == 60
    ramp = detect_anomaly(series([45 + (i % 2) for i in range(60)] + [50 + 4 * i for i in range(12)]), "memory_pct")
    assert ramp and ramp.pattern in ("ramp", "sawtooth")
    spike = detect_anomaly(series([0.1] * 60 + [0.1, 9, 0.1, 0.1, 0.1, 0.1]), "error_rate_pct")
    assert spike and spike.pattern == "spike"
    assert detect_anomaly(series(flat + flat[:10]), "latency_p99_ms") is None


def test_log_templates_keep_versions_and_mask_noise():
    t = to_template("duration: 1450.1 ms statement: SELECT 1 [application_name=checkout-service@2.14.0] id=9f8e7d6c5b")
    assert "checkout-service@2.14.0" in t and "<n>" in t and "<hex>" in t
    assert classify_signature("x509: certificate has expired")[0] == "certificate_expiry"
    assert classify_signature("could not acquire connection from pool")[0] == "resource_exhaustion"
    assert extract_refs("could not acquire connection (pool=orders-db, active=10/10)")["component"] == "orders-db"


def test_change_correlation_prefers_recent_relevant_changes():
    class C:
        def __init__(self, t, kind, component, details=None):
            self.t, self.kind, self.component, self.details = t, kind, component, details or {}

    near, _ = correlate_change(C(98, "deploy", "checkout-service"), 100, {"checkout-service"}, set(), False)
    far, _ = correlate_change(C(40, "deploy", "checkout-service"), 100, {"checkout-service"}, set(), False)
    unrelated, _ = correlate_change(C(98, "deploy", "search-service"), 100, {"checkout-service"}, set(), False)
    after, _ = correlate_change(C(110, "deploy", "checkout-service"), 100, {"checkout-service"}, set(), False)
    gradual, _ = correlate_change(C(75, "deploy", "checkout-service"), 100, {"checkout-service"}, set(), True)
    assert near > 0.8 > far and near > unrelated * 5 and after == 0 and gradual > 0.5


def test_tools_return_text_for_the_model_and_artifacts_for_code():
    w = World(BY_ID["checkout-deploy-regression"].spec(), ANCHOR)
    tools = build_tools(w)
    for names in ANALYST_TOOLS.values():
        assert all(n in tools for n in names)
    msg = tools["get_golden_signals"].invoke({"type": "tool_call", "id": "1", "name": "get_golden_signals",
                                              "args": {"component": "checkout-service"}})
    assert "ANOMALIES" in msg.content and msg.artifact["anomalies"]
    bad = tools["get_trace_breakdown"].invoke({"type": "tool_call", "id": "2", "name": "get_trace_breakdown",
                                               "args": {"component": "nope"}})
    assert "Unknown component" in bad.content


def test_runbooks_have_machine_readable_actions_and_are_retrievable():
    books = load_runbooks()
    assert len(books) >= 7
    assert all(a["action"] for d in books for a in d.metadata.get("actions", []))
    top = RunbookRetriever(k=1).invoke("connection pool exhausted too many clients")
    assert top[0].metadata["id"] == "RB-102"
