"""Drive the real LLM code path (langchain-anthropic -> anthropic SDK -> HTTP) against a local mock
of the Messages API, and check every request respects the current Claude API rules."""

import pytest

from argus.brains import _cache
from argus.sim import BY_ID
from mock_anthropic import MockAnthropic


@pytest.fixture
def mock_api(monkeypatch):
    mock = MockAnthropic().start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    _cache.clear()
    yield mock
    mock.stop()
    _cache.clear()


async def test_llm_engine_end_to_end(mock_api, run):
    events = []
    values, interrupts = await run(BY_ID["cart-bad-release"].spec(), reasoning="llm", collect=events)
    assert values["engine"] == "Claude Opus 5.5"
    assert values["status"] == "resolved"
    assert {f["engine"] for f in values["findings"]} == {"Claude Opus 5.5"}
    assert mock_api.violations == []
    reqs = mock_api.requests
    assert any(r.get("tools") for r in reqs), "investigators should call tools"
    assert any((r.get("output_config") or {}).get("format") for r in reqs), "structured outputs expected"
    assert any(r.get("stream") for r in reqs), "the postmortem should stream"
    assert {(r.get("output_config") or {}).get("effort") for r in reqs} == {"low", "medium", "high"}
    assert any(e["type"] == "report_token" for e in events)


async def test_risk_guardrail_overrides_the_model(mock_api, run):
    # the mock model labels a production rollback as "low" risk; the graph must raise it to the catalog floor
    _, interrupts = await run(BY_ID["cart-bad-release"].spec(), reasoning="llm")
    assert interrupts and interrupts[0]["plan"]["actions"][0]["risk"] == "high"


async def test_llm_failure_degrades_to_deterministic(monkeypatch, run):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    _cache.clear()
    from argus.brains import llm

    monkeypatch.setattr(llm.LLMBrain, "_llm", lambda self, role: (_ for _ in ()).throw(RuntimeError("provider down")))
    events = []
    values, _ = await run(BY_ID["auth-cert-expiry"].spec(), reasoning="llm", collect=events)
    _cache.clear()
    assert values["status"] == "resolved"
    assert any(e["type"] == "log" and e["agent"] == "Engine" for e in events)
